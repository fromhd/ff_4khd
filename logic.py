import os
import traceback
from .setup import P

class Logic:
    @staticmethod
    def plugin_load():
        P.logger.info("4KHD logic plugin_load")

    @staticmethod
    def plugin_unload():
        P.logger.info("4KHD logic plugin_unload")
