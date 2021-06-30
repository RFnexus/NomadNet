import RNS
import importlib
import time

import nomadnet
from nomadnet.ui.textui import *
from nomadnet import NomadNetworkApp

COLORMODE_MONO = 1
COLORMODE_16   = 16
COLORMODE_88   = 88
COLORMODE_256  = 256
COLORMODE_TRUE = 2**24
THEME_DARK     = 0x01
THEME_LIGHT    = 0x02

THEMES = {
    THEME_DARK: [
        # Style name                    # 16-color style                        # Monochrome style          # 88, 256 and true-color style
        ('heading',                     'light gray,underline', 'default',      'underline',                'g93,underline', 'default'),
        ('menubar',                     'black', 'light gray',                  'standout',                 '#111', '#bbb'),
        ('shortcutbar',                 'black', 'light gray',                  'standout',                 '#111', '#bbb'),
        ('body_text',                   'white', 'default',                     'default',                  '#ddd', 'default'),
        ('error_text',                  'dark red', 'default',                  'default',                  'dark red', 'default'),
        ('warning_text',                'yellow', 'default',                    'default',                  '#ba4', 'default'),
        ('inactive_text',               'dark gray', 'default',                 'default',                  'dark gray', 'default'),
        ('buttons',                     'light green,bold', 'default',          'default',                  '#00a533', 'default'),
        ('msg_editor',                  'black', 'light cyan',                  'standout',                 '#111', '#0bb'),
        ("msg_header_ok",               'black', 'light green',                 'standout',                 '#111', '#6b2'),
        ("msg_header_caution",          'black', 'yellow',                      'standout',                 '#111', '#fd3'),
        ("msg_header_sent",             'black', 'light gray',                  'standout',                 '#111', '#ddd'),
        ("msg_header_delivered",        'black', 'light blue',                  'standout',                 '#111', '#28b'),
        ("msg_header_failed",           'black', 'dark gray',                   'standout',                 'black', 'dark gray'),
        ("msg_warning_untrusted",       'black', 'dark red',                    'standout',                 '#111', 'dark red'),
        ("list_focus",                  "black", "light gray",                  "standout",                 "#111", "#bbb"),
        ("list_off_focus",              "black", "dark gray",                   "standout",                 "#111", "dark gray"),
        ("list_trusted",                "light green", "default",               "default",                  "#6b2", "default"),
        ("list_focus_trusted",          "black", "light gray",                  "standout",                 "#180", "#bbb"),
        ("list_unknown",                "dark gray", "default",                 "default",                  "light gray", "default"),
        ("list_untrusted",              "dark red", "default",                  "default",                  "dark red", "default"),
        ("list_focus_untrusted",        "black", "light gray",                  "standout",                 "#810", "#bbb"),
    ]
}

class TextUI:

    def __init__(self):
        self.app = NomadNetworkApp.get_shared_instance()
        self.app.ui = self
        self.loop = None

        if importlib.util.find_spec("urwid") != None:
            import urwid
        else:
            RNS.log("The text-mode user interface requires Urwid to be installed on your system.", RNS.LOG_ERROR)
            RNS.log("You can install it with the command: pip3 install urwid", RNS.LOG_ERROR)
            nomadnet.panic()

        urwid.set_encoding("UTF-8")

        intro_timeout = self.app.config["textui"]["intro_time"]
        colormode     = self.app.config["textui"]["colormode"]
        theme         = self.app.config["textui"]["theme"]
        mouse_enabled = self.app.config["textui"]["mouse_enabled"]

        palette       = THEMES[theme]

        self.screen = urwid.raw_display.Screen()
        self.screen.register_palette(palette)
        
        self.main_display = Main.MainDisplay(self, self.app)
        
        if intro_timeout > 0:
            self.intro_display = Extras.IntroDisplay(self.app)
            initial_widget = self.intro_display.widget
        else:
            initial_widget = self.main_display.widget

        self.loop = urwid.MainLoop(initial_widget, screen=self.screen, handle_mouse=mouse_enabled)

        if intro_timeout > 0:
            self.loop.set_alarm_in(intro_timeout, self.display_main)

        # TODO: Probably remove this at some point when better terminal
        # color capability detection has been implemented
        RNS.log("Starting Text UI in "+str(colormode)+" color mode. If no UI appears, try adjusting your color settings in "+str(self.app.configdir)+"/config", RNS.LOG_INFO)
        
        self.set_colormode(colormode)

        self.loop.run()

    def set_colormode(self, colormode):
        self.screen.set_terminal_properties(colormode)
        self.screen.reset_default_terminal_palette()

    def display_main(self, loop, user_data):
        self.loop.widget = self.main_display.widget
