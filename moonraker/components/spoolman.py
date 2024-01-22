# Integration with Spoolman
#
# Copyright (C) 2023 Daniel Hultgren <daniel.cf.hultgren@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
import logging
import os, sys
import re
import contextlib
import tornado.websocket as tornado_ws
from .file_manager.metadata import extract_metadata
from ..common import RequestType
from ..utils import json_wrapper as jsonw
from typing import (
    TYPE_CHECKING,
    List,
    Dict,
    Any,
    Optional,
    Union,
    cast
)

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from ..common import WebRequest
    from .http_client import HttpClient, HttpResponse
    from .database import MoonrakerDatabase
    from .announcements import Announcements
    from .klippy_apis import KlippyAPI as APIComp
    from tornado.websocket import WebSocketClientConnection

DB_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"
CONSOLE_TAB="   " # Special space characters used as they will be displayed in gcode console

class SpoolManager:
    def __init__(self, config: ConfigHelper):
        '''
        This class supercedes the SpoolManager class from moonraker/components/spoolman.py
        It aims at providing the same functionality, with added capability.
        Added capabilities are:
            - logging in the mainsail/fluidd console
            - Basic checks for filament presence
            - Basic check for filament type
            - Basic check for filament sufficience
        '''
        self.server = config.get_server()

        self.filament_slots = config.getint("filament_slots", default=1, minval=1)
        if self.filament_slots < 1 :
            self._log_n_send(f"Number of filament slots is not set or is less than 1. Please check the spoolman or moonraker [spoolman] setup.")
        self.printer_info = self.server.get_host_info()
        self.next_active_spool_update_time = 0.0
        self.eventloop = self.server.get_event_loop()
        self._get_spoolman_urls(config)
        self.sync_rate_seconds = config.getint("sync_rate", default=5, minval=1)
        self.extruded_lock = asyncio.Lock()
        self.spoolman_url = f"{config.get('server').rstrip('/')}/api"
        self.spool_id: Optional[int] = None
        self._error_logged: bool = False
        self._highest_epos: float = 0
        self._current_extruder: str = "extruder"
        self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
        self.http_client: HttpClient = self.server.lookup_component("http_client")
        self.database: MoonrakerDatabase = self.server.lookup_component("database")
        announcements: Announcements = self.server.lookup_component("announcements")
        announcements.register_feed("spoolman")
        self._register_notifications()
        self._register_listeners()
        self._register_endpoints()
        self.server.register_remote_method(
            "spoolman_set_active_spool", self.set_active_spool
        )
        self.server.register_remote_method(
            "spoolman_set_active_slot", self.set_active_slot
        )
        self.server.register_remote_method(
            "spoolman_get_spool_info", self.get_spool_info
        )
        self.server.register_remote_method(
            "spoolman_check_filament", self.check_filament
        )
        self.server.register_remote_method(
            "spoolman_get_spools_for_machine", self.get_spools_for_machine
        )
        self.server.register_remote_method(
            "spoolman_set_spool_slot", self.set_spool_slot
        )
        self.server.register_remote_method(
            "spoolman_clear_spool_slots", self.clear_spool_slots
        )

    def _get_spoolman_urls(self, config: ConfigHelper) -> None:
        orig_url = config.get('server')
        url_match = re.match(r"(?i:(?P<scheme>https?)://)?(?P<host>.+)", orig_url)
        if url_match is None:
            raise config.error(
                f"Section [spoolman], Option server: {orig_url}: Invalid URL format"
            )
        scheme = url_match["scheme"] or "http"
        host = url_match["host"].rstrip("/")
        ws_scheme = "wss" if scheme == "https" else "ws"
        self.spoolman_url = f"{scheme}://{host}/api"
        self.ws_url = f"{ws_scheme}://{host}/api/v1/spool"

    def _get_spoolman_urls(self, config: ConfigHelper) -> None:
        orig_url = config.get('server')
        url_match = re.match(r"(?i:(?P<scheme>https?)://)?(?P<host>.+)", orig_url)
        if url_match is None:
            raise config.error(
                f"Section [spoolman], Option server: {orig_url}: Invalid URL format"
            )
        scheme = url_match["scheme"] or "http"
        host = url_match["host"].rstrip("/")
        ws_scheme = "wss" if scheme == "https" else "ws"
        self.spoolman_url = f"{scheme}://{host}/api"
        self.ws_url = f"{ws_scheme}://{host}/api/v1/spool"

    def _register_notifications(self):
        self.server.register_notification("spoolman:active_spool_set")
        self.server.register_notification("spoolman:spoolman_status_changed")

    def _register_listeners(self):
        self.server.register_event_handler(
            "server:klippy_ready", self._handle_klippy_ready
        )

    def _register_endpoints(self):
        self.server.register_endpoint(
            "/server/spoolman/spool_id",
            RequestType.GET | RequestType.POST,
            self._handle_spool_id_request,
        )
        self.server.register_endpoint(
            "/server/spoolman/proxy",
            RequestType.POST,
            self._proxy_spoolman_request,
        )
        self.server.register_endpoint(
            "/server/spoolman/status",
            RequestType.GET,
            self._handle_status_request,
        )

    async def component_init(self) -> None:
        self.spool_id = await self.database.get_item(
            DB_NAMESPACE, ACTIVE_SPOOL_KEY, None
        )
        self.connection_task = self.eventloop.create_task(self._connect_websocket())

    async def _connect_websocket(self) -> None:
        log_connect: bool = True
        err_list: List[Exception] = []
        while not self.is_closing:
            if log_connect:
                logging.info(f"Connecting To Spoolman: {self.ws_url}")
                log_connect = False
            try:
                self.spoolman_ws = await tornado_ws.websocket_connect(
                    self.ws_url,
                    connect_timeout=5.,
                    ping_interval=20.,
                    ping_timeout=60.
                )
                setattr(self.spoolman_ws, "on_ping", self._on_ws_ping)
                cur_time = self.eventloop.get_loop_time()
                self._last_ping_received = cur_time
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if len(err_list) > 10:
                    # Allow up to 10 unique errors.
                    continue
                for err in err_list:
                    if type(err) is type(e) and err.args == e.args:
                        break
                else:
                    err_list.append(e)
                    verbose = self.server.is_verbose_enabled()
                    if verbose:
                        logging.exception("Failed to connect to Spoolman")
                    self.server.add_log_rollover_item(
                        "spoolman_connect", f"Failed to Connect to spoolman: {e}",
                        not verbose
                    )
            else:
                err_list = []
                self.ws_connected = True
                self._error_logged = False
                self.report_timer.start()
                self.server.add_log_rollover_item(
                    "spoolman_connect", "Connected to Spoolman Spool Manager"
                )
                if self.spool_id is not None:
                    self._cancel_spool_check_task()
                    coro = self._check_spool_deleted()
                    self.spool_check_task = self.eventloop.create_task(coro)
                self._send_status_notification()
                await self._read_messages()
                log_connect = True
            if not self.is_closing:
                await asyncio.sleep(self.reconnect_delay)

    async def _read_messages(self) -> None:
        message: Union[str, bytes, None]
        while self.spoolman_ws is not None:
            message = await self.spoolman_ws.read_message()
            if isinstance(message, str):
                self._decode_message(message)
            elif message is None:
                self.report_timer.stop()
                self.ws_connected = False
                cur_time = self.eventloop.get_loop_time()
                ping_time: float = cur_time - self._last_ping_received
                reason = code = None
                if self.spoolman_ws is not None:
                    reason = self.spoolman_ws.close_reason
                    code = self.spoolman_ws.close_code
                logging.info(
                    f"Spoolman Disconnected - Code: {code}, Reason: {reason}, "
                    f"Server Ping Time Elapsed: {ping_time}"
                )
                self.spoolman_ws = None
                if not self.is_closing:
                    self._send_status_notification()
                break

    def _decode_message(self, message: str) -> None:
        event: Dict[str, Any] = jsonw.loads(message)
        if event.get("resource") != "spool":
            return
        if self.spool_id is not None and event.get("type") == "deleted":
            payload: Dict[str, Any] = event.get("payload", {})
            if payload.get("id") == self.spool_id:
                self.pending_reports.pop(self.spool_id, None)
                self.set_active_spool(None)

    def _cancel_spool_check_task(self) -> None:
        if self.spool_check_task is None or self.spool_check_task.done():
            return
        self.spool_check_task.cancel()

    async def _check_spool_deleted(self) -> None:
        if self.spool_id is not None:
            response = await self.http_client.get(
                f"{self.spoolman_url}/v1/spool/{self.spool_id}",
                connect_timeout=1., request_timeout=2.
            )
            if response.status_code == 404:
                logging.info(f"Spool ID {self.spool_id} not found, setting to None")
                self.pending_reports.pop(self.spool_id, None)
                self.set_active_spool(None)
            elif response.has_error():
                err_msg = self._get_response_error(response)
                logging.info(f"Attempt to check spool status failed: {err_msg}")
            else:
                logging.info(f"Found Spool ID {self.spool_id} on spoolman instance")
        self.spool_check_task = None

    def connected(self) -> bool:
        return self.ws_connected

    def _on_ws_ping(self, data: bytes = b"") -> None:
        self._last_ping_received = self.eventloop.get_loop_time()

    async def _handle_klippy_ready(self) -> None:
        result: Dict[str, Dict[str, Any]]
        result = await self.klippy_apis.subscribe_objects(
            {"toolhead": ["position", "extruder"]}, self._handle_status_update, {}
        )
        initial_e_pos = self._eposition_from_status(result)
        logging.debug(f"Initial epos: {initial_e_pos}")
        if initial_e_pos is not None:
            self._highest_epos = initial_e_pos
        else:
            logging.error("Spoolman integration unable to subscribe to epos")
            raise self.server.error("Unable to subscribe to e position")

    def _get_response_error(self, response: HttpResponse) -> str:
        err_msg = f"HTTP error: {response.status_code} {response.error}"
        with contextlib.suppress(Exception):
            msg: Optional[str] = cast(dict, response.json())["message"]
            err_msg += f", Spoolman message: {msg}"
        return err_msg

    def _handle_status_update(self, status: Dict[str, Any], _: float) -> None:
        toolhead: Optional[Dict[str, Any]] = status.get("toolhead")
        if toolhead is None:
            return
        epos: float = toolhead.get("position", [0, 0, 0, self._highest_epos])[3]
        extr = toolhead.get("extruder", self._current_extruder)
        if extr != self._current_extruder:
            self._highest_epos = epos
            self._current_extruder = extr
        elif epos > self._highest_epos:
            if self.spool_id is not None:
                self._add_extrusion(self.spool_id, epos - self._highest_epos)
            self._highest_epos = epos

    def _add_extrusion(self, spool_id: int, used_length: float) -> None:
        if spool_id in self.pending_reports:
            self.pending_reports[spool_id] += used_length
        else:
            self.pending_reports[spool_id] = used_length

    def set_active_spool(self, spool_id: Union[int, None]) -> None:
        assert spool_id is None or isinstance(spool_id, int)
        if self.spool_id == spool_id:
            logging.info(f"Spool ID already set to: {spool_id}")
            return
        self.spool_id = spool_id
        self.database.insert_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, spool_id)
        self.server.send_event(
            "spoolman:active_spool_set", {"spool_id": spool_id}
        )
        logging.info(f"Setting active spool to: {spool_id}")

    async def set_active_slot(self, slot: int = None) -> None:
        '''
        Search for spool id matching the slot number and set it as active
        '''
        if slot is None:
            logging.error(f"Slot number not provided")
            return

        for spool in self.slot_occupation :
            logging.info(f"found spool: {spool['filament']['name']} at slot {spool['location'].split(':')[1]}")
            if int(spool['location'].split(':')[1]) == slot :
                await self.set_active_spool(spool['id'])
                return

        logging.error(f"Could not find a matching spool for slot {slot}")
        logging.info(f"Setting active spool to: {spool_id}")

    async def report_extrusion(self, eventtime: float) -> float:
        if not self.ws_connected:
            return eventtime + self.sync_rate_seconds
        pending_reports = self.pending_reports
        self.pending_reports = {}
        for spool_id, used_length in pending_reports.items():
            if not self.ws_connected:
                self._add_extrusion(spool_id, used_length)
                continue
            logging.debug(
                f"Sending spool usage: ID: {spool_id}, Length: {used_length:.3f}mm"
            )
            response = await self.http_client.request(
                method="PUT",
                url=f"{self.spoolman_url}/v1/spool/{spool_id}/use",
                body={"use_length": used_length}
            )
            if response.has_error():
                if response.status_code == 404:
                    # Since the spool is deleted we can remove any pending reports
                    # added while waiting for the request
                    self.pending_reports.pop(spool_id, None)
                    if spool_id == self.spool_id:
                        logging.info(f"Spool ID {spool_id} not found, setting to None")
                        self.set_active_spool(None)
                else:
                    if not self._error_logged:
                        error_msg = self._get_response_error(response)
                        self._error_logged = True
                        logging.info(
                            f"Failed to update extrusion for spool id {spool_id}, "
                            f"received {error_msg}"
                        )
                    # Add missed reports back to pending reports for the next cycle
                    self._add_extrusion(spool_id, used_length)
                    continue
            self._error_logged = False
        return self.eventloop.get_loop_time() + self.sync_rate_seconds

    async def _handle_spool_id_request(self, web_request: WebRequest):
        if web_request.get_request_type() == RequestType.POST:
            spool_id = web_request.get_int("spool_id", None)
            self.set_active_spool(spool_id)
        # For GET requests we will simply return the spool_id
        return {"spool_id": self.spool_id}

    async def _proxy_spoolman_request(self, web_request: WebRequest):
        method = web_request.get_str("request_method")
        path = web_request.get_str("path")
        query = web_request.get_str("query", None)
        body = web_request.get("body", None)
        use_v2_response = web_request.get_boolean("use_v2_response", False)
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise self.server.error(f"Invalid HTTP method: {method}")
        if body is not None and method == "GET":
            raise self.server.error("GET requests cannot have a body")
        if len(path) < 4 or path[:4] != "/v1/":
            raise self.server.error(
                "Invalid path, must start with the API version, e.g. /v1"
            )
        query = f"?{query}" if query is not None else ""
        full_url = f"{self.spoolman_url}{path}{query}"
        if not self.ws_connected:
            if not use_v2_response:
                raise self.server.error("Spoolman server not available", 503)
            return {
                "response": None,
                "error": {
                    "status_code": 503,
                    "message": "Spoolman server not available"
                }
            }
        logging.debug(f"Proxying {method} request to {full_url}")
        response = await self.http_client.request(
            method=method,
            url=full_url,
            body=body,
        )
        if not use_v2_response:
            response.raise_for_status()
            return response.json()
        if response.has_error():
            msg: str = str(response.error or "")
            with contextlib.suppress(Exception):
                spoolman_msg = cast(dict, response.json()).get("message", msg)
                msg = spoolman_msg
            return {
                "response": None,
                "error": {
                    "status_code": response.status_code,
                    "message": msg
                }
            }
        else:
            return {
                "response": response.json(),
                "error": None
            }

    async def _handle_status_request(self, web_request: WebRequest) -> Dict[str, Any]:
        pending: List[Dict[str, Any]] = [
            {"spool_id": sid, "filament_used": used} for sid, used in
            self.pending_reports.items()
        ]
        return {
            "spoolman_connected": self.ws_connected,
            "pending_reports": pending,
            "spool_id": self.spool_id
        }

    def _send_status_notification(self) -> None:
        self.server.send_event(
            "spoolman:spoolman_status_changed",
            {"spoolman_connected": self.ws_connected}
        )

    async def close(self):
        self.is_closing = True
        self.report_timer.stop()
        if self.spoolman_ws is not None:
            self.spoolman_ws.close(1001, "Moonraker Shutdown")
        self._cancel_spool_check_task()
        if self.connection_task is None or self.connection_task.done():
            return
        try:
            await asyncio.wait_for(self.connection_task, 2.)
        except asyncio.TimeoutError:
            pass
    async def _log_n_send(self, msg):
        ''' logs and sends msg to the klipper console'''
        logging.error(msg)
        await self.klippy_apis.run_gcode(f"M118 {msg}", None)

    async def get_info_for_spool(self, spool_id : int):
        logging.info(f"spool id received: {spool_id}")
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
        if not id :
            logging.info(f"Fetching active spool")
            spool_id = await self._get_active_spool()
        else:
            logging.info(f"Setting spool id: {id}")
            spool_id = id
        self.server.send_event(
            "spoolman:get_spool_info", {"id": spool_id}
        )
        if not spool_id :
            msg = f"No active spool set"
            await self._log_n_send(msg)
            return False

        spool_info = await self.get_info_for_spool(spool_id)
        msg = f"Active spool is: {spool_info['filament']['name']} (id : {spool_info['id']})"
        await self._log_n_send(msg)
        msg = f"{CONSOLE_TAB}- used: {int(spool_info['used_weight'])} g" # Special space characters used as they will be siplayed in gcode console
        await self._log_n_send(msg)
        msg = f"{CONSOLE_TAB}- remaining: {int(spool_info['remaining_weight'])} g" # Special space characters used as they will be siplayed in gcode console
        await self._log_n_send(msg)
        # if spool_id not in filament_slots :
        found = False
        for spoolid in self.slot_occupation:
            await self._log_n_send(msg)
            if int(spoolid['id']) == spool_id :
                msg = "{}- slot: {}".format(CONSOLE_TAB, int(spool_info['location'].split(self.printer_info["hostname"]+':')[1])) # Special space characters used as they will be siplayed in gcode console
                await self._log_n_send(msg)
                found = True
        if not found :
            msg = f"Spool id {spool_id} is not assigned to this machine"
            await self._log_n_send(msg)
            msg = f"Run : "
            await self._log_n_send(msg)
            msg = f"{CONSOLE_TAB}SET_SPOOL_SLOT ID={spool_id} SLOT=integer" # Special space characters used as they will be siplayed in gcode console
            await self._log_n_send(msg)
            return False

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
                    # if this spool is used for this print then there is a mismatch (else it's ok, but message is sent anyway)
                    await self._log_n_send(f"Filament mismatch spoolman vs slicer @id {tool_id}")
                    await self._log_n_send(f"{CONSOLE_TAB}- {sb_tools[tool_id]['filament']['name']} != {filament['name']}")
                    if filament['usage'] > 0 :
                        mismatch = True
                    else :
                        await self._log_n_send(f"{CONSOLE_TAB}  * This filament is not used during this print (not pausing the printer)")

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

    async def get_spools_for_machine(self, silent=False) -> [Dict[str, Any]]:
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
            if not silent : await self._log_n_send(f"Failed to retrieve spools from spoolman: {e}")
            return False
        if self.filament_slots < len(spools) :
            if not silent : await self._log_n_send(f"Number of spools assigned to machine {machine_hostname} is greater than the number of slots available on the machine. Please check the spoolman or moonraker [spoolman] setup.")
            return False
        if spools:
            if not silent : await self._log_n_send(f"Spools for machine:")
            # create a table of size len(spools)
            table = [None for x in range(len(spools))]
            for spool in spools:
                slot = spool['location'].split(machine_hostname+':')[1]
                if not slot :
                    if not silent : self._log_n_send(f"location field for {spool['filament']['name']} @ {spool['id']} in spoolman db is not formatted correctly. Please check the spoolman setup.")
                else :
                    table[int(slot)] = spool
            if not silent :
                for i, spool in enumerate(table) :
                    await self._log_n_send(f"{CONSOLE_TAB}{i} : {spool['filament']['name']}")
        else :
            if not silent : await self._log_n_send(f"No spools assigned to machine: {machine_hostname}")
            return False
        return spools

    async def unset_spool_slot(self, spool_id : int) -> bool:
        '''
        Removes the machine:slot allocation in spoolman db for spool_id

        parameters:
            @param spool_id: id of the spool to set
        returns:
            @return: True if successful, False otherwise
        '''
        if spool_id == None :
            await self._log_n_send(f"Trying to unset spool but no spool id provided.")
            return False

        #use the PATCH method on the spoolman api
        #get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(f"Unsetting spool {spool_id} for machine: {machine_hostname}")
        # get spool info from spoolman
        body = {
            "location"         : "",
        }
        args ={
            "request_method" : "PATCH",
            "path" : f"/v1/spool/{spool_id}",
            "body" : body,
        }
        webrequest = WebRequest(
            endpoint = f"{self.spoolman_url}/spools/{spool_id}",
            args=args,
            action="PATCH",
        )
        try :
            await self._proxy_spoolman_request(webrequest)
        except Exception as e:
            logging.error(f"Failed to unset spool {spool_id} for machine {machine_hostname}: {e}")
            await self._log_n_send(f"Failed to unset spool {spool_id} for machine {machine_hostname}")
            return False
        return True

    async def set_spool_slot(self, spool_id : int, slot : int=None) -> bool:
        '''
        Sets the spool with id=id for the current machine into optional slot number if mmu is enabled.

        parameters:
            @param spool_id: id of the spool to set
            @param slot: optional slot number to set the spool into. If not provided (and number of slots = 1), the spool will be set into slot 0.
        returns:
            @return: True if successful, False otherwise
        '''
        if spool_id == None :
            msg = f"Trying to set spool but no spool id provided."
            await self._log_n_send(msg)
            return False

        logging.info(f"Setting spool {spool_id} for machine: {self.printer_info['hostname']} @ slot {slot}")
        self.server.send_event(
            "spoolman:spoolman_set_spool_slot", {"id": spool_id, "slot": slot}
        )
        # check that slot not higher than number of slots available
        if (slot == None) and (self.filament_slots > 1) :
            msg = f"Trying to set spool {spool_id} for machine {self.printer_info['hostname']} but no slot number provided."
            await self._log_n_send(msg)
            return False
        elif (slot == None) and (self.filament_slots == 1) :
            slot = 0
        elif slot > self.filament_slots-1 :
            msg = f"Trying to set spool {spool_id} for machine {self.printer_info['hostname']} @ slot {slot} but only {self.filament_slots} slots are available. Please check the spoolman or moonraker [spoolman] setup."
            await self._log_n_send(msg)
            return False

        # first check if the spool is not already assigned to a machine
        spool_info = await self.get_info_for_spool(spool_id)
        if 'location' in spool_info:
            if spool_info['location'] != "" :
                if spool_info['location'].split(':')[0] == self.printer_info["hostname"] :
                    await self._log_n_send(f"Spool {spool_id} is already assigned to this machine @ slot {spool_info['location'].split(':')[1]}")
                    if int(spool_info['location'].split(':')[1]) == slot :
                        await self._log_n_send(f"Updating slot for spool {spool_info['filament']['name']} (id: {spool_id}) to {slot}")
                else :
                    await self._log_n_send(f"Spool {spool_id} is already assigned to another machine: {spool_info['location']}")
                    return False

        # then check that no spool is already assigned to the slot of this machine
        self.slot_occupation = await self.get_spools_for_machine(silent=True)
        spools = self.slot_occupation
        if spools not in [False, None]:
            for spool in spools :
                logging.info(f"found spool: {spool['filament']['name']} ")
                if int(spool['location'].split(':')[1]) == slot :
                    await self._log_n_send(f"Slot {slot} is already assigned to spool {spool['filament']['name']} (id: {spool['id']})")
                    await self._log_n_send(f"{CONSOLE_TAB}- Overwriting slot assignment")
                    if not await self.unset_spool_slot(spool['id']) :
                        await self._log_n_send(f"{CONSOLE_TAB*2}Failed to unset spool {spool['filament']['name']} (id: {spool['id']}) from slot {slot}")
                        return False
                    await self._log_n_send(f"{CONSOLE_TAB*2}Spool {spool['filament']['name']} (id: {spool['id']}) unset from slot {slot}")

        # Check if spool is not allready archived
        if spool_info['archived'] :
            msg = f"Spool {spool_id} is archived. Please check the spoolman setup."
            await self._log_n_send(msg)
            return False

        #use the PATCH method on the spoolman api
        #get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(f"Setting spool {spool_info['filament']['name']} (id: {spool_info['id']}) for machine: {machine_hostname} @ slot {slot}")
        # get spool info from spoolman
        spool_info = await self.get_info_for_spool(spool_id)
        body = {
            "location"         : f"{machine_hostname}:{slot}",
        }
        args ={
            "request_method" : "PATCH",
            "path" : f"/v1/spool/{spool_id}",
            "body" : body,
        }
        webrequest = WebRequest(
            endpoint = f"{self.spoolman_url}/spools/{spool_id}",
            args=args,
            action="PATCH",
        )
        try :
            await self._proxy_spoolman_request(webrequest)
        except Exception as e:
            logging.error(f"Failed to set spool {spool_id} for machine {machine_hostname} @ slot {slot}: {e}")
            await self._log_n_send(f"Failed to set spool {spool_id} for machine {machine_hostname}")
            return False
        await self._log_n_send(f"Spool {spool_id} set for machine {machine_hostname} @ slot {slot}")
        return True

    async def clear_spool_slots(self) -> bool:
        '''
        Clears all slots for the current machine
        '''
        logging.info(f"Clearing spool slots for machine: {self.printer_info['hostname']}")
        self.server.send_event(
            "spoolman:clear_spool_slots", {}
        )
        # get spools assigned to current machine
        spools = self.slot_occupation
        if spools not in [False, None]:
            for spool in spools :
                #use the PATCH method on the spoolman api
                #get current printer hostname
                machine_hostname = self.printer_info["hostname"]
                logging.info(f"Clearing spool {spool['id']} for machine: {machine_hostname}")
                # get spool info from spoolman
                __ = await self.get_info_for_spool(spool['id'])
                body = {
                    "location"         : "",
                }
                args ={
                    "request_method" : "PATCH",
                    "path" : f"/v1/spool/{spool['id']}",
                    "body" : body,
                }
                webrequest = WebRequest(
                    endpoint = f"{self.spoolman_url}/spools/{spool['id']}",
                    args=args,
                    action="PATCH",
                )
                try :
                    await self._proxy_spoolman_request(webrequest)
                except Exception as e:
                    logging.error(f"Failed to clear spool {spool['id']} for machine {machine_hostname}: {e}")
                    await self._log_n_send(f"Failed to clear spool {spool['id']} for machine {machine_hostname}")
                    return False
                await self._log_n_send(f"Spool {spool['id']} cleared for machine {machine_hostname}")
            return True
        else :
            msg = f"No spools for machine {self.printer_info['hostname']}"
            await self._log_n_send(msg)
            return False

    async def check_filament(self):
        '''
        Uses metadata from the gcode to identify the filaments and runs some verifications
        based on the filament type and the amount of filament left in spoolman db.
        '''
        logging.info(f"Checking filament")
        await self._log_n_send("="*32)
        await self._log_n_send(f"Checking filament consistency: ")
        await self._log_n_send("="*32)
        # verify that klipper is ready
        if self.server.get_klippy_state() != "ready":
            logging.error(f"Klippy not ready")
            self.server.send_event(
                "spoolman:check_failure", {"message" : "Klippy not ready"}
            )
            return False
        self.klippy_apis.pause_print()
        try:
            virtual_sdcard = await self.klippy_apis.query_objects({"virtual_sdcard": None})
            print_stats = await self.klippy_apis.query_objects({"print_stats": None})
        except Exception:
            # Klippy not connected
            logging.error(f"Klippy not retrieve virtual_sdcard or print_stats")
            self.server.send_event(
                "spoolman:check_failure", {"message" : f"Klippy not retrieve virtual_sdcard or print_stats"}
            )
            return False

        filename = os.path.join('/home', 'uboe', 'printer_data', 'gcodes', print_stats["print_stats"]["filename"])
        state = print_stats["print_stats"]["state"]

        if state not in ['printing', 'paused']:
            # No print active
            msg = f"No print active, cannot get gcode from file (state: {state})"
            await self._log_n_send(msg)
            self.server.send_event(
                "spoolman:check_failure", {"message" : msg}
            )
            return False

        # Get gcode from file
        if filename is None:
            logging.error(f"Filename is None")
            self.server.send_event(
                "spoolman:check_failure", {"message" : "Filename is None"}
            )
            return False

        metadata: Dict[str, Any] = {}
        if not filename:
            logging.info(f"No filemame retrieved: {filename}")
            sys.exit(-1)
        try:
            metadata = extract_metadata(filename, False)
        except Exception:
            raise Exception(f"Failed to extract metadata from {filename}")

        # check that active spool is in machine's slots
        active_spool = await self._get_active_spool()
        if active_spool is None:
            msg = f"No active spool set"
            await self._log_n_send(msg)
            self.server.send_event(
                "spoolman:check_failure", {"message" : msg}
            )
            return False

        if self.slot_occupation == False:
            if state not in ['paused', 'cancelled', 'complete', 'standby']:
                await self.klippy_apis.pause_print()
            msg = f"Failed to retrieve spools from spoolman"
            self.server.send_event(
                "spoolman:check_failure", {"message" : msg}
            )
            return False
        spools = self.slot_occupation
        found = False
        for spool in spools :
            if int(spool['id']) == active_spool :
                found = True
        if not found :
            await self._log_n_send(f"Active spool {active_spool} is not assigned to this machine")
            await self._log_n_send(f"Run : ")
            await self._log_n_send(f"{CONSOLE_TAB}SET_SPOOL_SLOT ID={active_spool} SLOT=integer")

        if not self.slot_occupation:
            if state not in ['paused', 'cancelled', 'complete', 'standby']:
                await self.klippy_apis.pause_print()
            msg = f"No spools assigned to machine {self.printer_info['hostname']}"
            self.server.send_event(
                "spoolman:check_failure", {"message" : msg}
            )
            return False

        ret = await self.verify_consistency(metadata, self.slot_occupation)
        if ret :
            msg = f"Slicer setup and spoolman db are consistent"
            await self._log_n_send(msg)
            self.klippy_apis.resume_print()
            return True
        else :
            msg1 = f"FILAMENT MISMATCH(ES) BETWEEN SPOOLMAN AND SLICER DETECTED! PAUSING PRINT."
            await self._log_n_send(msg1)
            msg2 = f"Please check the spoolman setup and physical spools to match the slicer setup."
            await self._log_n_send(msg2)
            #if printer is runnning, pause it
            if state not in ['paused', 'cancelled', 'complete', 'standby']:
                await self.klippy_apis.pause_print()
            self.server.send_event(
                "spoolman:check_failure", {"message" : msg1+msg2}
            )
            return False

    async def __spool_info_notificator(self, web_request: WebRequest):
        '''
        Sends spool info when requested by a webrequest
        '''
        spool_id = await self._get_active_spool()
        return await self.get_info_for_spool(spool_id)

def load_component(config: ConfigHelper) -> SpoolManager:
    return SpoolManager(config)
