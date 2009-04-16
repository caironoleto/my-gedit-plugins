#    Gedit file search plugin
#    Copyright (C) 2008  Oliver Gerlich <oliver.gerlich@gmx.de>
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 2 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program; if not, write to the Free Software
#    Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.


#
# Main classes:
# - FileSearchWindowHelper (is instantiated by FileSearchPlugin for every window, and holds the search dialog)
# - FileSearcher (is instantiated by FileSearchWindowHelper for every search, and holds the result tab)
# - FileSearchPlugin (the actual plugin, which implements the Gedit plugin interface)
#
# Search functionality classes:
# - SearchProcess (starts the external find/grep commands for searching, and reads the output)
# - GrepParser (accumulates output from grep command and parses it to extract files, line numbers, and lines)
#
# Helper classes:
# - ProcessInfo (gets process tree info, for killing search processes)
# - RecentList (holds list of recently-selected search directories, for search dialog)
# - SearchQuery (holds all parameters for a search; also, can read and write these from/to GConf)
#


import os
import gedit
import gtk
import gtk.glade
import gobject
import fcntl
import popen2
import re
import urllib
import gconf
import pango

ui_str = """<ui>
  <menubar name="MenuBar">
    <menu name="SearchMenu" action="Search">
      <placeholder name="SearchOps_2">
        <menuitem name="FileSearch" action="FileSearch"/>
      </placeholder>
    </menu>
  </menubar>
</ui>
"""

gconfBase = '/apps/gedit-2/plugins/file-search'


class ProcessInfo:
    """
    Parses the process table in /proc and offers info
    about processes and their parents.
    """
    def __init__ (self):

        self.pids = []

        intRe = re.compile('^\d+$')
        nameRe = re.compile('^Name:\s+(\w+)$')
        ppidRe = re.compile('^PPid:\s+(\d+)$')
        for d in os.listdir('/proc'):
            if intRe.match(d):
                pid = int(d)
                name = ''
                ppid = 0
                fileName = "/proc/%d/status" % pid
                try:
                    fd = open(fileName, "r")
                except IOError:
                    pass
                else:
                    for line in fd.readlines():
                        m = nameRe.match(line)
                        if m:
                            name = m.group(1)
                            continue
                        m = ppidRe.match(line)
                        if m:
                            ppid = int(m.group(1))
                            continue
                    self.pids.append( (pid, name, ppid) )

    def getName (self, mainPid):
        for pid in self.pids:
            if pid[0] == mainPid:
                return pid[1]
        return None

    def getDirectChildren (self, mainPid):
        res = []
        for pid in self.pids:
            if pid[2] == mainPid:
                res.append(pid[0])
        return res

    def getAllChildren (self, mainPid):
        "Returns a list of all (direct and indirect) child processes"
        res = []
        directChildren = self.getDirectChildren(mainPid)
        res.extend(directChildren)
        for pid in directChildren:
            res.extend( self.getAllChildren(pid) )
        return res


class RecentList:
    """
    Encapsulates a gtk.ListStore that stores a generic list of "most recently used entries"
    """
    def __init__ (self, gclient, confKey, maxEntries = 10):
        self.gclient = gclient
        self.confKey = gconfBase + "/" + confKey
        self.store = gtk.ListStore(str)
        self._maxEntries = maxEntries

        entries = self.gclient.get_list(self.confKey, gconf.VALUE_STRING)
        entries.reverse()
        for e in entries:
            if e and len(e) > 0:
                decodedName = urllib.unquote(e)
                self.add(decodedName, False)

        # TODO: also listen for gconf changes, and reload the list then

    def add (self, entrytext, doStore=True):
        "Add an entry that was just used."

        for row in self.store:
            if row[0] == entrytext:
                it = self.store.get_iter(row.path)
                self.store.remove(it)

        self.store.prepend([entrytext])

        if len(self.store) > self._maxEntries:
            it = self.store.get_iter(self.store[-1].path)
            self.store.remove(it)

        if doStore:
            entries = []
            for e in self.store:
                encodedName = urllib.quote(e[0])
                entries.append(encodedName)
            self.gclient.set_list(self.confKey, gconf.VALUE_STRING, entries)

    def isEmpty (self):
        return (len(self.store) == 0)

    def topEntry (self):
        if self.isEmpty():
            return None
        else:
            return self.store[0][0]


class SearchQuery:
    """
    Contains all parameters for a single search action.
    """
    def __init__ (self):
        self.text = ''
        self.directory = ''
        self.caseSensitive = True
        self.isRegExp = False
        self.includeSubfolders = True
        self.excludeHidden = True
        self.excludeBackup = True
        self.excludeVCS = True
        self.selectFileTypes = False
        self.fileTypeString = ''

    def parseFileTypeString (self):
        "Returns a list with the separate file globs from fileTypeString"
        return self.fileTypeString.split()

    def loadDefaults (self, gclient):
        try:
            self.caseSensitive = gclient.get_without_default(gconfBase+"/case_sensitive").get_bool()
        except:
            self.caseSensitive = True

        try:
            self.isRegExp = gclient.get_without_default(gconfBase+"/is_reg_exp").get_bool()
        except:
            self.isRegExp = False

        try:
            self.includeSubfolders = gclient.get_without_default(gconfBase+"/include_subfolders").get_bool()
        except:
            self.includeSubfolders = True

        try:
            self.excludeHidden = gclient.get_without_default(gconfBase+"/exclude_hidden").get_bool()
        except:
            self.excludeHidden = True

        try:
            self.excludeBackup = gclient.get_without_default(gconfBase+"/exclude_backup").get_bool()
        except:
            self.excludeBackup = True

        try:
            self.excludeVCS = gclient.get_without_default(gconfBase+"/exclude_vcs").get_bool()
        except:
            self.excludeVCS = True

        try:
            self.selectFileTypes = gclient.get_without_default(gconfBase+"/select_file_types").get_bool()
        except:
            self.selectFileTypes = False

    def storeDefaults (self, gclient):
        gclient.set_bool(gconfBase+"/case_sensitive", self.caseSensitive)
        gclient.set_bool(gconfBase+"/is_reg_exp", self.isRegExp)
        gclient.set_bool(gconfBase+"/include_subfolders", self.includeSubfolders)
        gclient.set_bool(gconfBase+"/exclude_hidden", self.excludeHidden)
        gclient.set_bool(gconfBase+"/exclude_backup", self.excludeBackup)
        gclient.set_bool(gconfBase+"/exclude_vcs", self.excludeVCS)
        gclient.set_bool(gconfBase+"/select_file_types", self.selectFileTypes)


class SearchProcess:
    """
    - starts the search command
    - asynchronously waits for output from search command
    - passes output to GrepParser
    - kills search command if requested
    """
    def __init__ (self, query, resultHandler):
        self.parser = GrepParser(resultHandler)

        directoryEsc = query.directory.replace('\\', '\\\\').replace('"', '\\"')

        findCmd = 'find "%s"' % directoryEsc
        if not(query.includeSubfolders):
            findCmd += """ -maxdepth 1"""
        if query.excludeHidden:
            findCmd += """ \( ! -path "%s*/.*" \)""" % directoryEsc
            findCmd += """ \( ! -path "%s.*" \)""" % directoryEsc
        if query.excludeBackup:
            findCmd += """ \( ! -name "*~" ! -name ".#*.*" \)"""
        if query.excludeVCS:
            findCmd += """ \( ! -path "*/CVS/*" ! -path "*/.svn/*" ! -path "*/.git/*" ! -path "*/RCS/*" \)"""
        if query.selectFileTypes:
            fileTypeList = query.parseFileTypeString()
            findCmd += """ \( -false"""
            for t in fileTypeList:
                findCmd += ' -o -name "%s"' % t.replace('\\', '\\\\\\\\').replace('"', '\\"')
            findCmd += """ \)"""
        findCmd += " -print0 2> /dev/null"

        grepCmd = " grep -H -I -n -s -Z"
        if not(query.caseSensitive):
            grepCmd += " -i"
        if not(query.isRegExp):
            grepCmd += " -F"
        grepCmd += """ -e "%s" 2> /dev/null""" % (query.text.replace('\\', '\\\\').replace('"', '\\"'))

        cmd = findCmd + " | xargs -0" + grepCmd
        #cmd = "sleep 2; echo -n 'abc'; sleep 3; echo 'xyz'; sleep 3"
        #cmd = "sleep 2"
        #cmd = "echo 'abc'"
        #print "executing command: %s" % cmd
        self.popenObj = popen2.Popen3(cmd)
        self.pipe = self.popenObj.fromchild

        # make pipe non-blocking:
        fl = fcntl.fcntl(self.pipe, fcntl.F_GETFL)
        fcntl.fcntl(self.pipe, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        #print "(add watch)"
        gobject.io_add_watch(self.pipe, gobject.IO_IN | gobject.IO_ERR | gobject.IO_HUP,
            self.onPipeReadable, priority=gobject.PRIORITY_LOW)

    def onPipeReadable (self, fd, cond):
        #print "condition: %s" % cond
        if (cond & gobject.IO_IN):
            readText = self.pipe.read(4000)
            #print "(read %d bytes)" % len(readText)
            if self.parser:
                self.parser.parseFragment(readText)
            return True
        else:
            # read all remaining data from pipe
            while True:
                readText = self.pipe.read(4000)
                #print "(read %d bytes before finish)" % len(readText)
                if len(readText) <= 0:
                    break
                if self.parser:
                    self.parser.parseFragment(readText)

            if self.parser:
                self.parser.finish()
            #print "(closing pipe)"
            result = self.pipe.close()
            if result == None:
                #print "(search finished successfully)"
                pass
            else:
                #print "(search finished with exit code %d; exited: %s, exit status: %d)" % (result,
                #str(os.WIFEXITED(result)), os.WEXITSTATUS(result))
                pass
            self.popenObj.wait()
            return False

    def cancel (self):
        #print "(cancelling search command)"
        mainPid = self.popenObj.pid
        pi = ProcessInfo()
        allProcs = [mainPid]
        allProcs.extend(pi.getAllChildren(mainPid))
        #print "main pid: %d; num procs: %d" % (mainPid, len(allProcs))
        for pid in allProcs:
            #print "killing pid %d (name: %s)" % (pid, pi.getName(pid))
            os.kill(pid, 15)
        self.parser.cancel()

    def destroy (self):
        """
        Force search process to stop as soon as possible, and ignore any further results.
        """
        self.cancel()
        self.parser = None


class GrepParser:
    """
    - buffers output from grep command
    - extracts full lines
    - parses lines for file name, line number, and line text
    - passes extracted info to resultHandler
    """
    def __init__ (self, resultHandler):
        self.buf = ""
        self.cancelled = False
        self.resultHandler = resultHandler

    def cancel (self):
        self.cancelled = True

    def parseFragment (self, text):
        if self.cancelled:
            return

        self.buf = self.buf + text

        while '\n' in self.buf:
            pos = self.buf.index('\n')
            line = self.buf[:pos]
            self.buf = self.buf[pos + 1:]
            self.parseLine(line)

    def parseLine (self, line):
        if self.cancelled:
            return

        filename = None
        lineno = None
        linetext = ""
        if '\0' in line:
            [filename, end] = line.split('\0', 1)
            if ':' in end:
                [lineno, linetext] = end.split(':', 1)
                lineno = int(lineno)

        if lineno == None:
            #print "(ignoring invalid line)"
            pass
        else:
            # Assume that grep output is in UTF8 encoding, and convert it to
            # a Unicode string. Also, sanitize non-UTF8 characters.
            # TODO: what's the actual encoding of grep's output?
            linetext = unicode(linetext, 'utf8', 'replace')
            #print "file: '%s'; line: %d; text: '%s'" % (filename, lineno, linetext)
            linetext = linetext.rstrip("\n\r")
            self.resultHandler.handleResult(filename, lineno, linetext)

    def finish (self):
        self.parseFragment("")
        if self.buf != "":
            self.parseLine(self.buf)
        self.resultHandler.handleFinished()


class FileSearchWindowHelper:
    def __init__(self, plugin, window):
        #print "file-search: plugin created for", window
        self._window = window
        self._plugin = plugin
        self._dialog = None
        self.searchers = [] # list of existing SearchProcess instances

        self.gclient = gconf.client_get_default()
        self.gclient.add_dir(gconfBase, gconf.CLIENT_PRELOAD_NONE)

        self._lastSearchTerms = RecentList(self.gclient, "recent_search_terms")
        self._lastDirs = RecentList(self.gclient, "recent_dirs")
        self._lastTypes = RecentList(self.gclient, "recent_types")

        self._lastDir = None

        self._lastClickIter = None # TextIter at position of last right-click or last popup menu

        self._insert_menu()

        self._window.connect_object("destroy", FileSearchWindowHelper.destroy, self)
        self._window.connect_object("tab-added", FileSearchWindowHelper.onTabAdded, self)
        self._window.connect_object("tab-removed", FileSearchWindowHelper.onTabRemoved, self)

    def deactivate(self):
        #print "file-search: plugin stopped for", self._window
        self.destroy()

    def destroy (self):
        #print "have to destroy %d existing searchers" % len(self.searchers)
        for s in self.searchers[:]:
            s.destroy()
        self._window = None
        self._plugin = None

    def update_ui(self):
        # Called whenever the window has been updated (active tab
        # changed, etc.)
        #print "file-search: plugin update for", self._window
        pass

    def onTabAdded (self, tab):
        handlerIds = []
        handlerIds.append( tab.get_view().connect_object("button-press-event", FileSearchWindowHelper.onButtonPress, self, tab) )
        handlerIds.append( tab.get_view().connect_object("popup-menu", FileSearchWindowHelper.onPopupMenu, self, tab) )
        handlerIds.append( tab.get_view().connect_object("populate-popup", FileSearchWindowHelper.onPopulatePopup, self, tab) )
        tab.set_data("file-search-handlers", handlerIds) # store list of handler IDs so we can later remove the handlers again

    def onTabRemoved (self, tab):
        handlerIds = tab.get_data("file-search-handlers")
        if handlerIds:
            for h in handlerIds:
                tab.get_view().handler_disconnect(h)
            tab.set_data("file-search-handlers", None)

    def onButtonPress (self, event, tab):
        if event.button == 3:
            (bufX, bufY) = tab.get_view().window_to_buffer_coords(gtk.TEXT_WINDOW_TEXT, int(event.x), int(event.y))
            self._lastClickIter = tab.get_view().get_iter_at_location(bufX, bufY)

    def onPopupMenu (self, tab):
        insertMark = tab.get_document().get_insert()
        self._lastClickIter = tab.get_document().get_iter_at_mark(insertMark)

    def onPopulatePopup (self, menu, tab):
        # add separator:
        sepMi = gtk.MenuItem()
        sepMi.show()
        menu.prepend(sepMi)

        # first check if user has selected some text:
        selText = ""
        currDoc = tab.get_document()
        selectionIters = currDoc.get_selection_bounds()
        if selectionIters and len(selectionIters) == 2:
            # Only use selected text if it doesn't span multiple lines:
            if selectionIters[0].get_line() == selectionIters[1].get_line():
                selText = selectionIters[0].get_text(selectionIters[1])

        # if no text is selected, use current word under cursor:
        if not(selText) and self._lastClickIter:
            startIter = self._lastClickIter.copy()
            if not(startIter.starts_word()):
                startIter.backward_word_start()
            endIter = startIter.copy()
            if endIter.inside_word():
                endIter.forward_word_end()
            selText = startIter.get_text(endIter)

        # add actual menu item:
        if selText:
            menuText = 'Search files for "%s"' % selText
        else:
            menuText = 'Search files...'
        mi = gtk.MenuItem(menuText, use_underline=False)
        mi.connect_object("activate", FileSearchWindowHelper.onMenuItemActivate, self, selText)
        mi.show()
        menu.prepend(mi)

    def onMenuItemActivate (self, searchText):
        self.openSearchDialog(searchText)

    def registerSearcher (self, searcher):
        self.searchers.append(searcher)

    def unregisterSearcher (self, searcher):
        self.searchers.remove(searcher)

    def _insert_menu(self):
        # Get the GtkUIManager
        manager = self._window.get_ui_manager()

        # Create a new action group
        self._action_group = gtk.ActionGroup("FileSearchPluginActions")
        self._action_group.add_actions([("FileSearch", "gtk-find", _("Find in files ..."),
                                         "<control><shift>F", _("Search in multiple files"),
                                         self.on_search_files_activate)])

        # Insert the action group
        manager.insert_action_group(self._action_group, -1)

        # Merge the UI
        self._ui_id = manager.add_ui_from_string(ui_str)

    def on_cboSearchTextEntry_changed (self, textEntry):
        """
        Is called when the search text entry is modified;
        disables the Search button whenever no search text is entered.
        """
        if textEntry.get_text() == "":
            self.tree.get_widget('btnSearch').set_sensitive(False)
        else:
            self.tree.get_widget('btnSearch').set_sensitive(True)

    def on_cbSelectFileTypes_toggled (self, checkbox):
        self.tree.get_widget('cboFileTypeList').set_sensitive( checkbox.get_active() )

    def on_btnBrowse_clicked (self, button):
        fileChooser = gtk.FileChooserDialog(title="Select directory to search in",
            parent=self._dialog,
            action=gtk.FILE_CHOOSER_ACTION_SELECT_FOLDER,
            buttons = (gtk.STOCK_CANCEL, gtk.RESPONSE_CANCEL, gtk.STOCK_OPEN, gtk.RESPONSE_OK))
        fileChooser.set_default_response(gtk.RESPONSE_OK)
        fileChooser.set_filename( self.tree.get_widget('cboSearchDirectoryEntry').get_text() )

        response = fileChooser.run()
        if response == gtk.RESPONSE_OK:
            selectedDir = os.path.normpath( fileChooser.get_filename() ) + "/"
            self.tree.get_widget('cboSearchDirectoryEntry').set_text(selectedDir)
        fileChooser.destroy()

    def on_search_files_activate(self, action):
        self.openSearchDialog()

    def openSearchDialog (self, searchText = None):
        gladeFile = os.path.join(os.path.dirname(__file__), "file-search.glade")
        self.tree = gtk.glade.XML(gladeFile)
        self.tree.signal_autoconnect(self)

        self._dialog = self.tree.get_widget('searchDialog')
        self._dialog.set_transient_for(self._window)

        #
        # set initial values for search dialog widgets
        #

        # find a nice default value for the search directory:
        searchDir = os.getcwdu()
        if self._lastDir != None:
            # if possible, use same directory as in last search:
            searchDir = self._lastDir
        else:
            # this is the first search since opening this Gedit window...
            if self._window.get_active_tab():
                # if ProjectMarker plugin has set a valid project root for the current file, use that:
                projectMarkerRootDir = self._window.get_active_tab().get_view().get_data("root_dir")
                if projectMarkerRootDir:
                    if projectMarkerRootDir.endswith("\n"):
                        projectMarkerRootDir = projectMarkerRootDir[:-1]
                    searchDir = projectMarkerRootDir
                else:
                    # otherwise, try to use directory of that file
                    currFileDir = self._window.get_active_tab().get_document().get_uri()
                    if currFileDir != None and currFileDir.startswith("file:///"):
                        searchDir = os.path.dirname(currFileDir[7:])
            else:
                # there's no file open => fall back to Gedit's current working dir
                pass

        searchDir = os.path.normpath(searchDir) + "/"

        # ... and display that in the text field:
        self.tree.get_widget('cboSearchDirectoryEntry').set_text(searchDir)

        # Fill the drop-down part of the text field with recent dirs:
        cboLastDirs = self.tree.get_widget('cboSearchDirectoryList')
        cboLastDirs.set_model(self._lastDirs.store)
        cboLastDirs.set_text_column(0)

        # TODO: the algorithm to select a good default search dir could probably be improved...

        if searchText == None:
            searchText = ""
            if self._window.get_active_tab():
                currDoc = self._window.get_active_document()
                selectionIters = currDoc.get_selection_bounds()
                if selectionIters and len(selectionIters) == 2:
                    # Only use selected text if it doesn't span multiple lines:
                    if selectionIters[0].get_line() == selectionIters[1].get_line():
                        searchText = selectionIters[0].get_text(selectionIters[1])
        self.tree.get_widget('cboSearchTextEntry').set_text(searchText)

        cboLastSearches = self.tree.get_widget('cboSearchTextList')
        cboLastSearches.set_model(self._lastSearchTerms.store)
        cboLastSearches.set_text_column(0)

        # Fill list of file types:
        cboLastTypes = self.tree.get_widget('cboFileTypeList')
        cboLastTypes.set_model(self._lastTypes.store)
        cboLastTypes.set_text_column(0)

        if not(self._lastTypes.isEmpty()):
            typeListString = self._lastTypes.topEntry()
            self.tree.get_widget('cboFileTypeEntry').set_text(typeListString)
        else:
            self.tree.get_widget('cboFileTypeEntry').set_text("*")


        # get default values for other controls from GConf:
        query = SearchQuery()
        query.loadDefaults(self.gclient)
        self.tree.get_widget('cbCaseSensitive').set_active(query.caseSensitive)
        self.tree.get_widget('cbRegExp').set_active(query.isRegExp)
        self.tree.get_widget('cbIncludeSubfolders').set_active(query.includeSubfolders)
        self.tree.get_widget('cbExcludeHidden').set_active(query.excludeHidden)
        self.tree.get_widget('cbExcludeBackups').set_active(query.excludeBackup)
        self.tree.get_widget('cbExcludeVCS').set_active(query.excludeVCS)
        self.tree.get_widget('cbSelectFileTypes').set_active(query.selectFileTypes)
        self.tree.get_widget('cboFileTypeList').set_sensitive( query.selectFileTypes )

        inputValid = False
        while not(inputValid):
            # display and run the search dialog (in a loop until all fields are correctly entered)
            result = self._dialog.run()
            if result != 1:
                self._dialog.destroy()
                return

            searchText = unicode(self.tree.get_widget('cboSearchTextEntry').get_text())
            searchDir = self.tree.get_widget('cboSearchDirectoryEntry').get_text()
            typeListString = self.tree.get_widget('cboFileTypeEntry').get_text()

            searchDir = os.path.expanduser(searchDir)
            searchDir = os.path.normpath(searchDir) + "/"

            if searchText == "":
                print "internal error: search text is empty!"
            elif not(os.path.exists(searchDir)):
                msgDialog = gtk.MessageDialog(self._dialog, gtk.DIALOG_MODAL | gtk.DIALOG_DESTROY_WITH_PARENT,
                    gtk.MESSAGE_ERROR, gtk.BUTTONS_OK, "Directory does not exist")
                msgDialog.format_secondary_text("The specified directory does not exist.")
                msgDialog.run()
                msgDialog.destroy()
            else:
                inputValid = True

        query.text = searchText
        query.directory = searchDir
        query.caseSensitive = self.tree.get_widget('cbCaseSensitive').get_active()
        query.isRegExp = self.tree.get_widget('cbRegExp').get_active()
        query.includeSubfolders = self.tree.get_widget('cbIncludeSubfolders').get_active()
        query.excludeHidden = self.tree.get_widget('cbExcludeHidden').get_active()
        query.excludeBackup = self.tree.get_widget('cbExcludeBackups').get_active()
        query.excludeVCS = self.tree.get_widget('cbExcludeVCS').get_active()
        query.selectFileTypes = self.tree.get_widget('cbSelectFileTypes').get_active()
        query.fileTypeString = typeListString

        self._dialog.destroy()

        #print "searching for '%s' in '%s'" % (searchText, searchDir)

        self._lastSearchTerms.add(searchText)
        self._lastDirs.add(searchDir)
        self._lastTypes.add(typeListString)
        query.storeDefaults(self.gclient)
        self._lastDir = searchDir

        searcher = FileSearcher(self._window, self, query)

class FileSearcher:
    """
    Gets a search query (and related info) and then handles everything related
    to that single file search:
    - creating a result window
    - starting grep (through SearchProcess)
    - displaying matches
    A FileSearcher object lives until its result panel is closed.
    """
    def __init__ (self, window, pluginHelper, query):
        self._window = window
        self.pluginHelper = pluginHelper
        self.pluginHelper.registerSearcher(self)
        self.query = query
        self.files = {}
        self.numMatches = 0
        self.numLines = 0
        self.wasCancelled = False

        self._createResultPanel()
        self._updateSummary()

        #searchSummary = "<span size=\"smaller\" foreground=\"#585858\">searching for </span><span size=\"smaller\"><i>%s</i></span><span size=\"smaller\" foreground=\"#585858\"> in </span><span size=\"smaller\"><i>%s</i></span>" % (query.text, query.directory)
        searchSummary = "<span size=\"smaller\">searching for <i>%s</i> in <i>%s</i></span>" % (
            escapeMarkup(query.text), escapeMarkup(gobject.filename_display_name(query.directory)))
        self.treeStore.append(None, [searchSummary, '', 0])

        self.searchProcess = SearchProcess(query, self)

    def handleResult (self, file, lineno, linetext):
        expandRow = False
        if not(self.files.has_key(file)):
            it = self._addResultFile(file)
            self.files[file] = it
            expandRow = True
        else:
            it = self.files[file]
        self._addResultLine(it, lineno, linetext)
        if expandRow:
            path = self.treeStore.get_path(it)
            self.treeView.expand_row(path, False)
        self._updateSummary()

    def handleFinished (self):
        #print "(finished)"
        self.searchProcess = None
        editBtn = self.tree.get_widget("btnModifyFileSearch")
        editBtn.hide()
        editBtn.set_label("gtk-edit")

        if self.wasCancelled:
            line = "<i><span foreground=\"red\">(search was cancelled)</span></i>"
        elif self.numMatches == 0:
            line = "<i>(no matching files found)</i>"
        else:
            if self.numMatches == 1:
                line = "<i>found 1 match"
            else:
                line = "<i>found %d matches" % self.numMatches

            if self.numLines == 1:
                line += " (1 line)"
            else:
                line += " (%d lines)" % self.numLines

            if len(self.files) == 1:
                line += " in 1 file</i>"
            else:
                line += " in %d files</i>" % len(self.files)
        self.treeStore.append(None, [line, '', 0])

    def _updateSummary (self):
        if self.numMatches == 1:
            summary = "<b>1</b> match"
        else:
            summary = "<b>%d</b> matches" % self.numMatches
        if len(self.files) == 1:
            summary += "\nin 1 file"
        else:
            summary += "\nin %d files" % len(self.files)
        self.tree.get_widget("lblNumMatches").set_label(summary)


    def _createResultPanel (self):
        gladeFile = os.path.join(os.path.dirname(__file__), "file-search.glade")
        self.tree = gtk.glade.XML(gladeFile, 'hbxFileSearchResult')
        self.tree.signal_autoconnect(self)
        resultContainer = self.tree.get_widget('hbxFileSearchResult')

        resultContainer.set_data("filesearcher", self)

        panel = self._window.get_bottom_panel()
        panel.add_item(resultContainer, self.query.text, "gtk-find")
        panel.activate_item(resultContainer)

        editBtn = self.tree.get_widget("btnModifyFileSearch")
        editBtn.set_label("gtk-stop")

        panel.set_property("visible", True)


        self.treeStore = gtk.TreeStore(str, str, int)
        self.treeView = self.tree.get_widget('tvFileSearchResult')
        self.treeView.set_model(self.treeStore)

        self.treeView.set_search_equal_func(resultSearchCb)

        tc = gtk.TreeViewColumn("File", gtk.CellRendererText(), markup=0)
        self.treeView.append_column(tc)

    def _addResultFile (self, filename):
        dispFilename = filename
        # remove leading search directory part if present:
        if dispFilename.startswith(self.query.directory):
            dispFilename = dispFilename[ len(self.query.directory): ]
            dispFilename.lstrip("/")
        dispFilename = gobject.filename_display_name(dispFilename)

        (directory, file) = os.path.split( dispFilename )
        if directory:
            directory = os.path.normpath(directory) + "/"

        line = "%s<b>%s</b>" % (escapeMarkup(directory), escapeMarkup(file))
        it = self.treeStore.append(None, [line, filename, 0])
        return it

    def _addResultLine (self, it, lineno, linetext):
        addTruncationMarker = False
        if len(linetext) > 1000:
            linetext = linetext[:1000]
            addTruncationMarker = True

        if not(self.query.isRegExp):
            (linetext, numLineMatches) = escapeAndHighlight(linetext, self.query.text, self.query.caseSensitive)
            self.numMatches += numLineMatches
        else:
            linetext = escapeMarkup(linetext)
            self.numMatches += 1
        self.numLines += 1

        if addTruncationMarker:
            linetext += "</span><span size=\"smaller\"><i> [...]</i>"
        line = "<b>%d:</b> <span foreground=\"blue\">%s</span>" % (lineno, linetext)
        self.treeStore.append(it, [line, None, lineno])

    def on_row_activated (self, widget, path, col):
        selectedIter = self.treeStore.get_iter(path)
        parentIter = self.treeStore.iter_parent(selectedIter)
        lineno = 0
        if parentIter == None:
            file = self.treeStore.get_value(selectedIter, 1)
        else:
            file = self.treeStore.get_value(parentIter, 1)
            lineno = self.treeStore.get_value(selectedIter, 2)

        if not(file):
            return

        uri="file://%s" % urllib.quote(file)
        gedit.commands.load_uri(window=self._window, uri=uri, line_pos=lineno)
        if lineno > 0: # this is necessary for Gedit 2.17.4 and older (see gbo #401219)
            currDoc = self._window.get_active_document()
            currDoc.goto_line(lineno - 1) # -1 required to work around gbo #503665
            currView = gedit.tab_get_from_document(currDoc).get_view()
            currView.scroll_to_cursor()

    def on_btnClose_clicked (self, button):
        self.destroy()

    def destroy (self):
        if self.searchProcess:
            self.searchProcess.destroy()
            self.searchProcess = None

        panel = self._window.get_bottom_panel()
        resultContainer = self.tree.get_widget('hbxFileSearchResult')
        resultContainer.set_data("filesearcher", None)
        panel.remove_item(resultContainer)
        self.treeStore = None
        self.treeView = None
        self._window = None
        self.files = {}
        self.tree = None
        self.pluginHelper.unregisterSearcher(self)

    def on_btnModify_clicked (self, button):
        if not(self.searchProcess):
            # edit search params
            pass
        else:
            # cancel search
            self.searchProcess.cancel()
            self.wasCancelled = True

    def on_tvFileSearchResult_button_press_event (self, treeview, event):
        if event.button == 3:
            path = treeview.get_path_at_pos(int(event.x), int(event.y))
            if path != None:
                treeview.grab_focus()
                treeview.set_cursor(path[0], path[1], False)

                menu = gtk.Menu()
                mi = gtk.ImageMenuItem("gtk-copy")
                mi.connect_object("activate", FileSearcher.onPopupMenuItemActivate, self, treeview, path[0])
                mi.show()
                menu.append(mi)

                menu.popup(None, None, None, event.button, event.time)
                return True
        else:
            return False

    def onPopupMenuItemActivate (self, treeview, path):
        it = treeview.get_model().get_iter(path)
        markupText = treeview.get_model().get_value(it, 0)
        plainText = pango.parse_markup(markupText, u'\x00')[1]

        clipboard = gtk.clipboard_get()
        clipboard.set_text(plainText)
        clipboard.store()


def resultSearchCb (model, column, key, it):
    """Callback function for searching in result list"""
    lineText = model.get_value(it, column)
    plainText = pango.parse_markup(lineText, u'\x00')[1] # remove Pango markup

    # for file names, add a leading slash before matching:
    parentIter = model.iter_parent(it)
    if parentIter == None and not(plainText.startswith("/")):
        plainText = "/" + plainText

    # if search text contains only lower-case characters, do case-insensitive matching:
    if key.islower():
        plainText = plainText.lower()

    # if the line contains the search text, it matches:
    if plainText.find(key) >= 0:
        return False

    # line doesn't match:
    return True


def escapeMarkup (origText):
    "Replaces Pango markup special characters with their escaped replacements"
    text = origText
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    return text

def escapeAndHighlight (origText, searchText, caseSensitive):
    """
    Replaces Pango markup special characters, and adds highlighting markup
    around text fragments that match searchText.
    """

    # split origText by searchText; the resulting list will contain normal text
    # and matching text interleaved (if two matches are adjacent in origText,
    # they will be separated by an empty string in the resulting list).
    matchLen = len(searchText)
    if not(caseSensitive):
        searchText = searchText.lower()
    fragments = []
    startPos = 0
    text = origText[:]
    while True:
        if not(caseSensitive):
            pos = text.lower().find(searchText, startPos)
        else:
            pos = text.find(searchText, startPos)
        if pos < 0:
            break
        preStr = origText[startPos:pos]
        matchStr = origText[pos:pos+matchLen]
        fragments.append(preStr)
        fragments.append(matchStr)
        startPos = pos+matchLen
    fragments.append(text[startPos:])

    numMatches = (len(fragments) - 1) / 2

    if len(fragments) < 3:
        print "too few fragments (got only %d)" % len(fragments)
        print "text: '%s'" % origText.encode("utf8", "replace")
        numMatches += 1
    #assert(len(fragments) > 2)

    # join fragments again, adding markup around matches:
    retText = ""
    highLight = False
    for f in fragments:
        f = escapeMarkup(f)
        if highLight:
            retText += "<span background=\"#FFFF00\">%s</span>" % f
        else:
            retText += f
        highLight = not(highLight)
    return (retText, numMatches)


class FileSearchPlugin(gedit.Plugin):
    def __init__(self):
        gedit.Plugin.__init__(self)
        self._instances = {}

    def activate(self, window):
        self._instances[window] = FileSearchWindowHelper(self, window)

    def deactivate(self, window):
        self._instances[window].deactivate()
        del self._instances[window]

    def update_ui(self, window):
        self._instances[window].update_ui()
