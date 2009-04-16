# -*- coding: utf-8 -*-
#  Simple save current editing session
# 
#  Copyright (C) 2008 Andrew Gryaznov
#  email any feedback or suggestions to: realgrandrew@gmail.com
#   
#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#   
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#   
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place, Suite 330,
#  Boston, MA 02111-1307, USA.

VERSION = "0.2b"

import gedit, gtk
from gettext import gettext as _
import cPickle, os

# Menu item example, insert a new item in the Tools menu
ui_str = """<ui>
  <menubar name="MenuBar">
    <menu name="ToolsMenu" action="Tools">
      <placeholder name="ToolsOps_2">
        <menuitem name="SessionSave" action="SessionSave"/>
      </placeholder>
      <placeholder name="ToolsOps_2">
        <menuitem name="SessionRestore" action="SessionRestore"/>
      </placeholder>
    </menu>
  </menubar>
</ui>
"""

class SessionPluginInstance:
	def __init__(self, plugin, window):
		self._window = window
		self._plugin = plugin
		self.enc = gedit.gedit_encoding_get_current()
		self.docs=[]
		self.olddocs = []
		# Insert menu items
		self._insert_menu()
		try:
		  os.mkdir(os.path.expanduser('~/.gnome2/gedit'))
		except:
		  pass

	def stop(self):
		# Remove any installed menu items
		self._remove_menu()
		
		self._window = None
		self._plugin = None
		self._action_group = None
		os.remove(os.path.expanduser('~/.gnome2/gedit/sessionsave.dump'))
		
	def _insert_menu(self):
		# Get the GtkUIManager
		manager = self._window.get_ui_manager()
		
		# Create a new action group
		self._action_group = gtk.ActionGroup("SessionPluginActions")
		self._action_group.add_actions([("SessionSave", None, _("Session Save"), None, _("Session Save"), lambda a: self.on_save())])
		self._action_group.add_actions([("SessionRestore", None, _("Session Restore"), None, _("Session Restore"), lambda b: self.on_restore())])
		
		# Insert the action group
		manager.insert_action_group(self._action_group, -1)
		
		# Merge the UI
		self._ui_id = manager.add_ui_from_string(ui_str)

	def _remove_menu(self):
		# Get the GtkUIManager
		manager = self._window.get_ui_manager()
		
		# Remove the ui
		manager.remove_ui(self._ui_id)
		
		# Remove the action group
		manager.remove_action_group(self._action_group)
		
		# Make sure the manager updates
		manager.ensure_update()

	def update(self):
		# Called whenever the window has been updated (active tab
		# changed, etc.)
		newdocs = self._window.get_documents()
		if (len(newdocs) > len(self.docs)) or (self.olddocs == newdocs):
		  self.on_save()
		else:
		  self.olddocs = newdocs
		return

	
	def on_save(self):
		cdocs = self._window.get_documents()
		self.docs = [d.get_uri() for d in cdocs]
		fn = os.path.expanduser('~/.gnome2/gedit/sessionsave.dump')
		fd = open(fn, 'w')
		cPickle.dump(self.docs, fd)
		fd.close()
		
	
	def on_restore(self):
		fn = os.path.expanduser('~/.gnome2/gedit/sessionsave.dump')
		self.docs = cPickle.load(open(fn, 'r'))
		
		for uri in self.docs:
		  if  not uri is None: self._window.create_tab_from_uri(uri, self.enc, 0, False, False)
	  # TODO: search for "TODO HERE" and goto_line to there (like first analyze the file then load it) 


class SessionPlugin(gedit.Plugin):
	DATA_TAG = "SessionPluginInstance"
	
	def __init__(self):
		gedit.Plugin.__init__(self)

	def _get_instance(self, window):
		return window.get_data(self.DATA_TAG)
	
	def _set_instance(self, window, instance):
		window.set_data(self.DATA_TAG, instance)
	
	def activate(self, window):
		self._set_instance(window, SessionPluginInstance(self, window))
		self._get_instance(window).on_restore()
	
	def deactivate(self, window):
		self._get_instance(window).stop()
		self._set_instance(window, None)
		
	def update_ui(self, window):
		self._get_instance(window).update()
