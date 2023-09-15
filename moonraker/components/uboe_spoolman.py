# Integration with Spoolman
#
# Copyright (C) 2023 Daniel Hultgren <daniel.cf.hultgren@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations
from .spoolman import *
from .file_manager.metadata import extract_metadata

import asyncio
import os
import sys
import datetime
import logging
from moonraker.websockets import WebRequest

from typing import (
    TYPE_CHECKING,
    Awaitable,
    Optional,
    Dict,
    List,
    Union,
    Any,
)
if TYPE_CHECKING:
    from ..app import InternalTransport
    from ..confighelper import ConfigHelper
    from ..websockets import WebsocketManager
    from ..common import BaseRemoteConnection
    from tornado.websocket import WebSocketClientConnection
    from .database import MoonrakerDatabase
    from .klippy_apis import KlippyAPI
    from .job_state import JobState
    from .machine import Machine
    from .file_manager.file_manager import FileManager
    from .http_client import HttpClient
    from .power import PrinterPower
    from .announcements import Announcements
    from .webcam import WebcamManager, WebCam
    from ..klippy_connection import KlippyConnection

class UboeSpoolManager(SpoolManager):
    '''
    This class supercedes the SpoolManager class from moonraker/components/spoolman.py
    It aims at providing the same functionality, with added capability.
    Added capabilities are:
       - logging in the mainsail/fluidd console
       - Basic checks for filament presence
       - Basic check for filament type
       - Basic check for filament sufficience
    '''
    def __init__(self, config: ConfigHelper):
        super().__init__(config)
        self.next_active_spool_update_time = 0.0
        self._register_notifications()
        self.server.register_remote_method(
            "spoolman_get_spool_info", self.get_spool_info
        )
        self.server.register_remote_method(
            "spoolman_check_filament", self.check_filament
        )

    def _register_notifications(self):
        super()._register_notifications()
        self.server.register_notification("spoolman:active_spool_get")
        self.server.register_notification("spoolman:check_filament")

    async def get_info_for_spool(self, spool_id):
        self.server.send_event(
            "spoolman:active_spool_get", {"spool_id": spool_id}
        )
        logging.info(f"Active spool received: {spool_id}")
        args ={
            "request_method" : "GET",
            "path" : f"/v1/spool/{spool_id}",
            # "query" : f"spool_id={spool_id}",

        }
        webrequest = WebRequest(
            endpoint = f"{self.spoolman_url}/spools/{spool_id}",
            args=args,
            action="GET",
        )
        spool_info = await self._proxy_spoolman_request(webrequest)
        return spool_info

    async def get_spool_info(self):
        '''
        Gets info for active spool
        '''
        logging.info(f"Fetching active spool")
        spool_id = await self._get_active_spool()
        spool_info = await self.get_info_for_spool(spool_id)
        logging.info(f"Spool info: {spool_info}")
        await self.klippy_apis.run_gcode(f"M118 Active spool is: {spool_info}", None)

    async def _get_active_spool(self):
        spool_id = await self.database.get_item(
            DB_NAMESPACE, ACTIVE_SPOOL_KEY, None
        )
        return spool_id

    async def verify_consistency(self, metadata, active_spool):
        '''
        Verifies that the filament type, name, color and amount are consistent with the spoolman db
        '''
        pass

    async def get_spools_for_machine(self, printer_info) -> List[Dict[str, Any]]:
        '''
        Gets all spools assigned to the current machine
        '''
        # get current printer hostname
        # machine_hostname = printer_info["info"]["hostname"]
        logging.info(f"Getting spools for machine: {printer_info}")
        return

        if machine_hostname is None:
            machine_hostname = await self.machine.get_machine_hostname()
        args ={
            "request_method" : "GET",
            "path" : f"/v1/spools",
            "query" : f"machine_hostname={machine_hostname}",

        }
        webrequest = WebRequest(
            endpoint = f"{self.spoolman_url}/spools",
            args=args,
            action="GET",
        )
        spools = await self._proxy_spoolman_request(webrequest)
        return spools

    async def check_filament(self):
        '''
        Uses metadata from the gcode to identify the filaments and runs some verifications
        based on the filament type and the amount of filament left in spoolman db.
        '''
        self.server.send_event(
            "spoolman:active_spool_get", {}
        )
        logging.info(f"Checking filament")
        # verify that klipper is ready
        if self.server.get_klippy_state() != "ready":
            logging.error(f"Klippy not ready")
            return False
        kapi: KlippyAPI = self.server.lookup_component("klippy_apis")
        try:
            is_active = await kapi.query_objects({"virtual_sdcard": None})
            filename = await kapi.query_objects({"print_stats": None})
            printer_info = await kapi.query_objects({"printer.info": None})
        except Exception:
            # Klippy not connected
            logging.error(f"Klippy not retrieve is_active or filename")
            return False

        is_active = is_active["virtual_sdcard"]['is_active']
        filename = filename["print_stats"]["filename"]

        # if is_active not in ['printing', 'paused']: # !TODO : Reactiviate this
        #     # No print active
        #     logging.error(f"No print active, cannot get gcode from file")
        #     return False

        # Get gcode from file
        if filename is None:
            logging.error(f"Filename is None")
            return False

        metadata: Dict[str, Any] = {}
        # if not filename: # !TODO : Reactiviate this
        #     logging.info(f"No filemame retrieved: {filename}")
        #     sys.exit(-1)
        filename = os.path.join('/', 'home', 'uboe', 'Cone_ASA_9m43s.gcode')
        try:
            metadata = extract_metadata(filename, False)
        except Exception:
            raise Exception(f"Failed to extract metadata from {filename}")

        # Get spools assigned to current machine
        spools = await self.get_spools_for_machine(printer_info)
        # active_spool = await self.get_active_spool()

        # await self.verify_consistency(metadata, active_spool)


def load_component(config: ConfigHelper) -> UboeSpoolManager:
    return UboeSpoolManager(config)
