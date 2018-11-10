#  Copyright 2018, Michael DeHaan LLC
#  License: Apache License Version 2.0 + Commons Clause
#  -------------------------------------------------------------------------
#  output.py - applies all output plugins to a message. Output plugins
#  can be used to format build output or also redirect it to additional
#  sources.
#  --------------------------------------------------------------------------

from vespene.common.plugin_loader import PluginLoader

class OutputManager(object):

    def __init__(self):
        self.plugin_loader = PluginLoader()
        self.plugins = self.plugin_loader.get_output_plugins()

    def get_msg(self, build, msg):
        for p in self.plugins:
            msg = p.filter(build, msg)
        return msg
    