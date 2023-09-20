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
        self.is_mmu = config.getboolean("is_mmu", default=False)
        self.printer_info = self.server.get_host_info()
        self.next_active_spool_update_time = 0.0
        self._register_notifications()
        self.server.register_remote_method(
            "spoolman_get_spool_info", self.get_spool_info
        )
        self.server.register_remote_method(
            "spoolman_check_filament", self.check_filament
        )
        self.server.register_remote_method(
            "spoolman_get_spools_for_machine", self.get_spools_for_machine
        )

    async def _log_n_send(self, msg):
        ''' logs and sends msg to the klipper console'''
        logging.error(msg)
        await self.klippy_apis.run_gcode(f"M118 {msg}", None)

    def _register_notifications(self):
        super()._register_notifications()
        self.server.register_notification("spoolman:get_spool_info")
        self.server.register_notification("spoolman:check_filament")

    async def get_info_for_spool(self, spool_id):
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

    async def get_spool_info(self, id : int=None):
        '''
        Gets info for active spool id and sends it to the klipper console
        '''
        logging.info(f"Fetching active spool")
        if not id:
            spool_id = await self._get_active_spool()
        else:
            spool_id = id
        self.server.send_event(
            "spoolman:get_spool_info", {"spool_id": spool_id}
        )
        spool_info = await self.get_info_for_spool(spool_id)
        msg = f"Active spool is: {spool_info['filament']['name']} (id : {spool_info['id']})"
        await self._log_n_send(msg)
        msg = f"   used: {int(spool_info['used_weight'])} g"
        await self._log_n_send(msg)
        msg = f"   remaining: {int(spool_info['remaining_weight'])} g"
        await self._log_n_send(msg)

    async def _get_active_spool(self):
        spool_id = await self.database.get_item(
            DB_NAMESPACE, ACTIVE_SPOOL_KEY, None
        )
        return spool_id

    async def verify_consistency(self, metadata, spools):
        '''
        Verifies that the filament type, name, color and amount are consistent with the spoolman db
        parameters:
            @param metadata: metadata extracted from the gcode file
            @param spools: list of spools assigned to the current machine retrieved from spoolman db
        '''
        # location field in spoolman db is the <hostname of the machine>:<tool_id>
        # tool_id is 0 for single extruder machines
        # build a list of all the tools assigned to the current machine
        sb_tools = {}
        for spool in spools :
            tool_id = spool["location"].split(":")[1]
            sb_tools[int(tool_id)] = spool

        mdata_filaments = metadata["filament_name"].replace("\"", "").replace("\n", "").split(";")
        mdata_filament_usage = metadata["filament_used"].replace("\"", "").replace("\n", "").split(",")

        # build the equivalent list for the gcode metadata
        metadata_tools = {}
        for id, filament in enumerate(mdata_filaments):
            if not filament == 'EMPTY' :
                metadata_tools[id] = {'name' : filament, 'usage' : float(mdata_filament_usage[id])}
            elif filament == 'EMPTY' and not (float(mdata_filament_usage[id]) == 0) :
                msg = f"Filament usage for tool {id} is not 0 but filament is EMPTY placeholder. Please check your slicer setup and regenerate the gcode file."
                await self._log_n_send(msg)
                return False
            elif filament == 'EMPTY' and (float(mdata_filament_usage[id]) == 0) :
                # seems coherent
                pass
            else :
                # everything is fine
                pass

        # compare the two lists
        mismatch = False
        # check list length
        if len(sb_tools) != len(metadata_tools):
            msg = f"Number of tools mismatch between spoolman slicer and klipper: {len(sb_tools)} != {len(metadata_tools)}"
            mismatch = True
            await self._log_n_send(msg)

        # check filaments names for each tool
        for tool_id, filament in metadata_tools.items():
            # if tool_id from slicer is not in spoolman db
            if tool_id not in sb_tools :
                msg = f"Tool id {tool_id} of machine {self.printer_info['hostname']} not assigned to a spool in spoolman db"
                mismatch = True
                await self._log_n_send(msg)
            else :
                # if filament name from slicer is not the same as the one in spoolman db
                if sb_tools[tool_id]['filament']['name'] != filament['name']:
                    msg = f"Filament mismatch spoolman vs slicer @id {tool_id}: {sb_tools[tool_id]['filament']['name']} != {filament['name']}"
                    mismatch = True
                    await self._log_n_send(msg)

        if mismatch:
            return False

        # check that the amount of filament left in the spool is sufficient
        # get the amount of filament needed for each tool
        for tool_id, filament in metadata_tools.items():
            if filament['usage'] > sb_tools[tool_id]['remaining_weight']:
                msg = f"WARNING : Filament amount insufficient for spool {filament['name']}: {sb_tools[tool_id]['remaining_weight']*100/100} < {filament['usage']*100/100}"
                mismatch = True
                await self._log_n_send(msg)
                msg = f"Expect filament runout for machine {self.printer_info['hostname']}, or setup the mmu in order to avoid this."
                await self._log_n_send(msg)
        if mismatch:
            return False

        return True

    async def get_spools_for_machine(self) -> List[Dict[str, Any]]:
        '''
        Gets all spools assigned to the current machine
        '''
        # get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(f"Getting spools for machine: {machine_hostname}")

        args ={
            "request_method" : "GET",
            "path" : f"/v1/spool",
            "query" : f"location={machine_hostname}",

        }
        webrequest = WebRequest(
            endpoint = f"{self.spoolman_url}/spools",
            args=args,
            action="GET",
        )
        try :
            spools = await self._proxy_spoolman_request(webrequest)
        except Exception as e:
            await self._log_n_send(f"Failed to retrieve spools from spoolman: {e}")
            return False
        if not self.is_mmu and len(spools) > 1:
            await self._log_n_send(f"More than one spool assigned to machine: {machine_hostname} but MMU is not enabled")
            return False
        await self._log_n_send(f"Spools for machine:")
        for spool in spools:
            index = spool['location'].split(machine_hostname+':')[1]
            if not index :
                self._log_n_send(f"location field for {spool['filament']['name']} @ {spool['id']} in spoolman db is not formatted correctly. Please check the spoolman setup.")
            await self._log_n_send(f"   {spool['filament']['name']} (index : {spool['id']})")
        return spools

    async def check_filament(self):
        '''
        Uses metadata from the gcode to identify the filaments and runs some verifications
        based on the filament type and the amount of filament left in spoolman db.
        '''
        self.server.send_event(
            "spoolman:check_filament", {}
        )
        logging.info(f"Checking filament")
        # verify that klipper is ready
        if self.server.get_klippy_state() != "ready":
            logging.error(f"Klippy not ready")
            return False
        kapi: KlippyAPI = self.server.lookup_component("klippy_apis")
        try:
            virtual_sdcard = await kapi.query_objects({"virtual_sdcard": None})
            print_stats = await kapi.query_objects({"print_stats": None})
        except Exception:
            # Klippy not connected
            logging.error(f"Klippy not retrieve virtual_sdcard or print_stats")
            return False

        is_active = virtual_sdcard["virtual_sdcard"]['is_active']
        filename = os.path.join('/home', 'uboe', 'printer_data', 'gcodes', print_stats["print_stats"]["filename"])
        state = print_stats["print_stats"]["state"]

        if state not in ['printing', 'paused']:
            # No print active
            msg = f"No print active, cannot get gcode from file (state: {state})"
            await self._log_n_send(msg)
            return False

        # Get gcode from file
        if filename is None:
            logging.error(f"Filename is None")
            return False

        metadata: Dict[str, Any] = {}
        if not filename:
            logging.info(f"No filemame retrieved: {filename}")
            sys.exit(-1)
        try:
            metadata = extract_metadata(filename, False)
        except Exception:
            raise Exception(f"Failed to extract metadata from {filename}")

        # Get spools assigned to current machine
        ret = await self.get_spools_for_machine()
        if ret == False:
            return False
        spools = ret
        if not spools:
            msg = f"No spools assigned to machine: {self.printer_info['hostname']}"
            await self._log_n_send(msg)
            return False

        ret = await self.verify_consistency(metadata, spools)
        if ret :
            msg = f"Slicer setup and spoolman db are consistent"
            await self._log_n_send(msg)
            return True
        else :
            msg = f"FILAMENT MISMATCH(ES) BETWEEN SPOOLMAN AND SLICER DETECTED! PAUSING PRINT."
            await self._log_n_send(msg)
            msg = f"Please check the spoolman setup and physical spools to match the slicer setup."
            await self._log_n_send(msg)
            #if printer is runnning, pause it
            if state not in ['paused', 'cancelled', 'complete', 'standby']:
                await self.klippy_apis.run_gcode(f"PAUSE", None)
            return False


def load_component(config: ConfigHelper) -> UboeSpoolManager:
    return UboeSpoolManager(config)
