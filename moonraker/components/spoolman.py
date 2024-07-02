# Integration with Spoolman
#
# Copyright (C) 2023 Daniel Hultgren <daniel.cf.hultgren@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from __future__ import annotations
import asyncio
from asyncio.log import logger
import logging
import os
import sys
import re
import contextlib
import tornado.websocket as tornado_ws
from .file_manager.metadata import extract_metadata
from ..common import RequestType, HistoryFieldData
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

from ..common import WebRequest
if TYPE_CHECKING:
    from ..confighelper import ConfigHelper
    from .http_client import HttpClient, HttpResponse
    from .database import MoonrakerDatabase
    from .announcements import Announcements
    from .klippy_apis import KlippyAPI as APIComp
    from .history import History
    from tornado.websocket import WebSocketClientConnection

DB_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "spoolman.spool_id"
# Special space characters used as they will be displayed in gcode console
CONSOLE_TAB = "   " #!FIXME : should probably be done differently


class SpoolManager:
    def __init__(self, config: ConfigHelper):
        '''
        Capabilities are:
            - logging in the mainsail/fluidd console
            - Basic checks for filament presence
            - Basic check for filament type
            - Basic check for filament sufficience
            - Spool swap table generation
            - Spool swap table verification
        '''
        self.server = config.get_server()
        self.eventloop = self.server.get_event_loop()
        self._get_spoolman_urls(config)
        self.sync_rate_seconds = config.getint("sync_rate", default=5, minval=1)
        self.report_timer = self.eventloop.register_timer(self.report_extrusion)
        self.pending_reports: Dict[int, float] = {}
        self.spoolman_ws: Optional[WebSocketClientConnection] = None
        self.connection_task: Optional[asyncio.Task] = None
        self.spool_check_task: Optional[asyncio.Task] = None
        self.ws_connected: bool = False
        self.reconnect_delay: float = 2.
        self.is_closing: bool = False
        self.spool_id: Optional[int] = None
        self._error_logged: bool = False
        self._highest_epos: float = 0
        self._current_extruder: str = "extruder"
        self.spool_history = HistoryFieldData(
            "spool_ids", "spoolman", "Spool IDs used", "collect",
            reset_callback=self._on_history_reset
        )
        history: History = self.server.lookup_component("history")
        history.register_auxiliary_field(self.spool_history)
        self.klippy_apis: APIComp = self.server.lookup_component("klippy_apis")
        self.http_client: HttpClient = self.server.lookup_component("http_client")
        self.database: MoonrakerDatabase = self.server.lookup_component("database")
        announcements: Announcements = self.server.lookup_component("announcements")
        announcements.register_feed("spoolman")
        self._register_notifications()
        self._register_listeners()
        self._register_endpoints()
        self.filament_slots = config.getint(
            "filament_slots", default=1, minval=1)
        self.slot_occupation = {}
        if self.filament_slots < 1 :
            logging.critical(f"Number of filament slots is less than 1. Please check the spoolman or moonraker [spoolman] setup.")
            return
        self.printer_info = self.server.get_host_info()
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
            "spoolman_unset_spool_slot", self.unset_spool_slot
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

    def _register_notifications(self):
        self.server.register_notification("spoolman:active_spool_set")
        self.server.register_notification("spoolman:spoolman_status_changed")
        self.server.register_notification("spoolman:check_failure")
        self.server.register_notification("spoolman:spoolman_set_spool_slot")
        self.server.register_notification("spoolman:get_spool_info")

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
        self.server.register_endpoint(
            "/access/spoolman/info",
            RequestType.GET,
            self.__spool_info_notificator,
        )

    def _on_history_reset(self) -> List[int]:
        if self.spool_id is None:
            return []
        return [self.spool_id]

    async def component_init(self) -> None:
        self.spool_id = await self.database.get_item(
            DB_NAMESPACE, ACTIVE_SPOOL_KEY, None
        )
        self.connection_task = self.eventloop.create_task(
            self._connect_websocket())

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
                if len(err_list) < 10:
                    # Allow up to 10 unique errors.
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
        await self.get_spools_for_machine(silent=False)
        result: Dict[str, Dict[str, Any]]
        result = await self.klippy_apis.subscribe_objects(
            {"toolhead": ["position", "extruder"]}, self._handle_status_update, {}
        )
        toolhead = result.get("toolhead", {})
        self._current_extruder = toolhead.get("extruder", "extruder")
        initial_e_pos = toolhead.get("position", [None]*4)[3]
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
        self.spool_history.tracker.update(spool_id)
        self.spool_id = spool_id
        self.database.insert_item(DB_NAMESPACE, ACTIVE_SPOOL_KEY, spool_id)
        self.server.send_event(
            "spoolman:active_spool_set", {"spool_id": spool_id}
        )
        logging.info(f"Setting active spool to: {spool_id}")

    async def set_active_slot(self, slot = None) -> None:
        '''
        Search for spool id matching the slot number and set it as active
        '''
        if slot is None:
            logging.error(f"Slot number not provided")
            return

        for spool in self.slot_occupation:
            logging.info(
                f"found spool: {spool['filament']['name']} at slot {spool['location'].split(':')[1]}")
            if int(spool['location'].split(':')[1]) == slot:
                self.set_active_spool(spool['id'])
                return

        logging.error(f"Could not find a matching spool for slot {slot}")

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
                "response_headers": dict(response.headers.items()),
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

    #!FIXME : should probably be removed once debugging needs decrease
    async def _log_n_send(self, msg):
        ''' logs and sends msg to the klipper console'''
        logging.error(msg)
        await self.klippy_apis.run_gcode(f"M118 {msg}", None)

    async def get_info_for_spool(self, spool_id : int):
        response = await self.http_client.request(
                method="GET",
                url=f"{self.spoolman_url}/v1/spool/{spool_id}",
            )
        if response.status_code == 404:
            logging.info(f"Spool ID {spool_id} not found")
            return False
        elif response.has_error():
            err_msg = self._get_response_error(response)
            logging.info(f"Attempt to get spool info failed: {err_msg}")
            return False
        else:
            logging.info(f"Found Spool ID {self.spool_id} on spoolman instance")

        spool_info = response.json()
        logging.info(f"Spool info: {spool_info}")

        return spool_info

    async def get_spool_info(self, sid: int = None):
        '''
        Gets info for active spool id and sends it to the klipper console
        '''
        if not sid:
            logging.info("Fetching active spool")
            spool_id = await self._get_active_spool()
        else:
            logging.info("Setting spool id: %s", sid)
            spool_id = sid
        self.server.send_event(
            "spoolman:get_spool_info", {"id": spool_id}
        )
        if not spool_id:
            msg = "No active spool set"
            await self._log_n_send(msg)
            return False

        spool_info = await self.get_info_for_spool(spool_id)
        if not spool_info :
            msg = f"Spool id {spool_id} not found"
            await self._log_n_send(msg)
            return False
        msg = f"Active spool is: {spool_info['filament']['name']} (id : {spool_info['id']})"
        await self._log_n_send(msg)
        msg = f"{CONSOLE_TAB}- used: {int(spool_info['used_weight'])} g" # Special space characters used as they will be displayed in gcode console
        await self._log_n_send(msg)
        msg = f"{CONSOLE_TAB}- remaining: {int(spool_info['remaining_weight'])} g" # Special space characters used as they will be displayed in gcode console
        await self._log_n_send(msg)
        # if spool_id not in filament_slots :
        found = False
        for spoolid in self.slot_occupation:
            if int(spoolid['id']) == spool_id :
                msg = "{}- slot: {}".format(CONSOLE_TAB, int(spool_info['location'].split(self.printer_info["hostname"]+':')[1])) # Special space characters used as they will be displayed in gcode console
                await self._log_n_send(msg)
                found = True
        if not found:
            msg = f"Spool id {spool_id} is not assigned to this machine"
            await self._log_n_send(msg)
            msg = "Run : "
            await self._log_n_send(msg)
            msg = f"{CONSOLE_TAB}SET_SPOOL_SLOT ID={spool_id} SLOT=integer" # Special space characters used as they will be displayed in gcode console
            await self._log_n_send(msg)
            return False

    async def _get_active_spool(self):
        spool_id = await self.database.get_item(
            DB_NAMESPACE, ACTIVE_SPOOL_KEY, None
        )
        return spool_id

    async def verify_consistency(self, metadata, spools) -> List[str, list]:
        '''
        Verifies that the filament type, name, color and amount are consistent with the spoolman db
        parameters:
            @param metadata: metadata extracted from the gcode file
            @param spools: list of spools assigned to the current machine retrieved from spoolman db
        return :
            @return: a list containing the severity of the mismatch and a swap table to replace tool ids in gcode
        '''
        mismatch = "warning"
        # location field in spoolman db is the <hostname of the machine>:<tool_id>
        # tool_id is 0 for single extruder machines
        # build a list of all the tools assigned to the current machine
        sm_tools = {}
        for spool in spools:
            tool_id = spool["location"].split(":")[1]
            sm_tools[int(tool_id)] = spool

        mdata_filaments = metadata["filament_name"].replace(
            "\"", "").replace("\n", "").split(";")
        mdata_filament_usage = metadata["filament_used"].replace(
            "\"", "").replace("\n", "").split(",")

        # build the equivalent list for the gcode metadata
        metadata_tools = {}
        for i, filament in enumerate(mdata_filaments):
            fil_usage = float(mdata_filament_usage[i]) if len(
                mdata_filament_usage) > i else 0
            if not filament == 'EMPTY':
                metadata_tools[i] = {'name': filament, 'usage': fil_usage}
            elif filament == 'EMPTY' and not (fil_usage == 0):
                mismatch = "critical"
                msg = f"Filament usage for tool {i} is not 0 but filament is EMPTY placeholder. Please check your slicer setup and regenerate the gcode file."
                await self._log_n_send((mismatch).upper()+': '+mismatch+': '+msg)
                return mismatch, []
            elif filament == 'EMPTY' and (fil_usage == 0):
                # seems coherent
                pass
            else:
                # everything is fine
                pass

        # compare the two lists
        # check list length
        if len(sm_tools) != len(metadata_tools):
            msg = f"Number of tools mismatch between spoolman slicer and klipper: {len(sm_tools)} != {len(metadata_tools)}"
            await self._log_n_send((mismatch).upper()+': '+msg)

        # check filaments names for each tool
        swap_table = [None for __ in range(self.filament_slots)]
        for tool_id, filament in metadata_tools.items():
            # if tool_id from slicer is not in spoolman db
            if tool_id not in sm_tools:
                msg = f"Tool id {tool_id} of machine {self.printer_info['hostname']} not assigned to a spool in spoolman db"
                await self._log_n_send((mismatch).upper()+': '+msg)
                if filament['usage'] > 0:
                    # if this filament can be found on another slot, raise a warning and save the new slot number to replace in gcode later
                    found = False
                    for spool in spools:
                        if spool['filament']['name'] == filament['name']:
                            found = True
                            msg = f"Filament {filament['name']} is found in another slot: {spool['location'].split(':')[1]}"
                            swap_table[tool_id] = int(
                                spool['location'].split(':')[1])
                    if not found:
                        mismatch = "critical"
                        msg = f"Filament {filament['name']} not found on any other slot"
                        await self._log_n_send((mismatch).upper()+': '+msg)
            else:
                # if filament name from slicer is not the same as the one in spoolman db
                if sm_tools[tool_id]['filament']['name'] != filament['name']:
                    if filament['usage'] > 0:
                        # if this filament can be found on another slot, raise a warning and save the new slot number to replace in gcode later
                        found = False
                        for spool in spools:
                            if spool['filament']['name'] == filament['name']:
                                found = True
                                msg = f"Filament {filament['name']} is found in another slot: {spool['location'].split(':')[1]}"
                                swap_table[tool_id] = int(
                                    spool['location'].split(':')[1])
                        if not found:
                            mismatch = "critical"
                            msg = f"Filament {filament['name']} not found on any other slot"
                            await self._log_n_send((mismatch).upper()+': '+msg)
                    else:
                        await self._log_n_send((mismatch).upper()+f": Filament {filament['name']} is not used during this print (not pausing the printer)")

        # verify swap table is consistent (not more than one index pointing to same value)
        for i in range(len(swap_table)):
            if swap_table.count(swap_table[i]) > 1 and swap_table[i] != None:
                msg = f"Swap table is not consistent: {swap_table}, more than one slot has been swapped to same slot."
                mismatch = "critical"
                await self._log_n_send((mismatch).upper()+': '+msg)

        if mismatch == "critical":
            return mismatch, swap_table

        # check that the amount of filament left in the spool is sufficient
        # get the amount of filament needed for each tool
        for tool_id, filament in metadata_tools.items():
            # if the tool has been swapped, use the new tool id
            _tool_id = tool_id
            if swap_table[int(tool_id)]:
                _tool_id = swap_table[tool_id]
            # else use the original tool id and verify that the amount of filament left is sufficient
            if (_tool_id in sm_tools) and (filament['usage'] > sm_tools[_tool_id]['remaining_weight']):
                msg = f"Filament amount insufficient for spool {filament['name']}: {sm_tools[_tool_id]['remaining_weight']*100/100} < {filament['usage']*100/100}"
                mismatch = "critical"
                await self._log_n_send((mismatch).upper()+': '+msg)
                msg = f"Expect filament runout for machine {self.printer_info['hostname']}, or setup the mmu in order to avoid this."
                await self._log_n_send((mismatch).upper()+': '+msg)
        if mismatch == "critical":
            return mismatch, swap_table

        # Check that the active spool matches the spool from metadata when in single extruder mode
        if self.filament_slots == 1 and len(sm_tools) == 1:
            if self.spool_id != sm_tools[0]['id']:
                msg = f"Active spool mismatch: {self.spool_id} != {sm_tools[0]['id']}"
                mismatch = "critical"
                await self._log_n_send((mismatch).upper()+': '+msg)
                return mismatch

        return mismatch, swap_table

    async def get_spools_for_machine(self, silent=False) -> List[Dict[str, Any]]:
        '''
        Gets all spools assigned to the current machine
        '''
        # get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(f"Getting spools for machine: {machine_hostname}")

        args = {
            "request_method": "GET",
            "path": f"/v1/spool",
            "query": f"location={machine_hostname}",

        }
        webrequest = WebRequest(
            endpoint=f"{self.spoolman_url}/spools",
            args=args,
            request_type=RequestType.GET,
        )
        try:
            spools = await self._proxy_spoolman_request(webrequest)
        except Exception as e:
            if not silent :
                await self._log_n_send(f"Failed to retrieve spools from spoolman: {e}")
            return []
        if self.filament_slots < len(spools) :
            if not silent :
                await self._log_n_send(f"Number of spools assigned to machine {machine_hostname} is greater than the number of slots available on the machine. Please check the spoolman or moonraker [spoolman] setup.")
            return []
        if spools:
            if not silent:
                await self._log_n_send("Spools for machine:")
            # create a table of size len(spools)
            table = [None for __ in range(self.filament_slots)]
            for spool in spools:
                slot = spool['location'].split(machine_hostname+':')[1]
                if not slot :
                    if not silent :
                        await self._log_n_send(f"location field for {spool['filament']['name']} @ {spool['id']} in spoolman db is not formatted correctly. Please check the spoolman setup.")
                else :
                    table[int(slot)] = spool
            if not silent:
                for i, spool in enumerate(table):
                    if spool:
                        await self._log_n_send(f"{CONSOLE_TAB}{i} : {spool['filament']['name']}")
                    else:
                        await self._log_n_send(f"{CONSOLE_TAB}{i} : empty")
        if not silent and not spools:
            await self._log_n_send(f"No spools assigned to machine: {machine_hostname}")
        self.slot_occupation = spools
        await self.klippy_apis.run_gcode("SAVE_VARIABLE VARIABLE=mmu_slots VALUE=\"{}\"".format(self.slot_occupation))
        return False

    async def unset_spool_id(self, spool_id: int) -> bool:
        '''
        Removes the machine:slot allocation in spoolman db for spool_id

        parameters:
            @param spool_id: id of the spool to set
        returns:
            @return: True if successful, False otherwise
        '''
        if spool_id == None:
            await self._log_n_send(f"Trying to unset spool but no spool id provided.")
            return False

        # use the PATCH method on the spoolman api
        # get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(
            f"Unsetting spool if {spool_id} for machine: {machine_hostname}")
        # get spool info from spoolman
        body = {
            "location": "",
        }
        args = {
            "request_method": "PATCH",
            "path": f"/v1/spool/{spool_id}",
            "body": body,
        }
        webrequest = WebRequest(
            endpoint=f"{self.spoolman_url}/spools/{spool_id}",
            args=args,
            request_type=RequestType.POST,
        )
        try:
            await self._proxy_spoolman_request(webrequest)
        except Exception as e:
            logging.error(
                f"Failed to unset spool {spool_id} for machine {machine_hostname}: {e}")
            await self._log_n_send(f"Failed to unset spool {spool_id} for machine {machine_hostname}")
            return False
        await self.get_spools_for_machine(silent=True)
        return True

    async def set_spool_slot(self, spool_id : int, slot : int) -> bool:
        '''
        Sets the spool with id=id for the current machine into optional slot number if mmu is enabled.

        parameters:
            @param spool_id: id of the spool to set
            @param slot: optional slot number to set the spool into. If not provided (and number of slots = 1), the spool will be set into slot 0.
        returns:
            @return: True if successful, False otherwise
        '''
        await self.get_spools_for_machine(silent=True)
        if spool_id == None:
            msg = f"Trying to set spool but no spool id provided."
            await self._log_n_send(msg)
            return False

        logging.info(
            f"Setting spool {spool_id} for machine: {self.printer_info['hostname']} @ slot {slot}")
        self.server.send_event(
            "spoolman:spoolman_set_spool_slot", {"id": spool_id, "slot": slot}
        )
        # check that slot not higher than number of slots available
        if (slot == None) and (self.filament_slots > 1):
            msg = f"Trying to set spool {spool_id} for machine {self.printer_info['hostname']} but no slot number provided."
            await self._log_n_send(msg)
            return False
        elif not slot and (self.filament_slots == 1):
            slot = 0
        elif slot > self.filament_slots-1:
            msg = f"Trying to set spool {spool_id} for machine {self.printer_info['hostname']} @ slot {slot} but only {self.filament_slots} slots are available. Please check the spoolman or moonraker [spoolman] setup."
            await self._log_n_send(msg)
            return False

        # first check if the spool is not already assigned to a machine
        spool_info = await self.get_info_for_spool(spool_id)
        if 'location' in spool_info:
            if spool_info['location']:
                # if the spool is already assigned to current machine
                if spool_info['location'].split(':')[0] == self.printer_info["hostname"]:
                    await self._log_n_send(f"Spool {spool_info['filament']['name']} (id: {spool_id}) is already assigned to this machine @ slot {spool_info['location'].split(':')[1]}")
                    if int(spool_info['location'].split(':')[1]) == slot:
                        await self._log_n_send(f"Updating slot for spool {spool_info['filament']['name']} (id: {spool_id}) to {slot}")
                # if the spool is already assigned to another machine
                else:
                    await self._log_n_send(f"Spool {spool_info['filament']['name']} (id: {spool_id}) is already assigned to another machine: {spool_info['location']}")
                    return False

        # then check that no spool is already assigned to the slot of this machine
        if self.slot_occupation not in [False, None]:
            for spool in self.slot_occupation:
                logging.info(f"found spool: {spool['filament']['name']} ")
                if int(spool['location'].split(':')[1]) == slot:
                    await self._log_n_send(f"Slot {slot} is already assigned to spool {spool['filament']['name']} (id: {spool['id']})")
                    await self._log_n_send(f"{CONSOLE_TAB}- Overwriting slot assignment")
                    if not await self.unset_spool_id(spool['id']):
                        await self._log_n_send(f"{CONSOLE_TAB*2}Failed to unset spool {spool['filament']['name']} (id: {spool['id']}) from slot {slot}")
                        return False
                    await self._log_n_send(f"{CONSOLE_TAB*2}Spool {spool['filament']['name']} (id: {spool['id']}) unset from slot {slot}")

        # Check if spool is not allready archived
        if spool_info['archived']:
            msg = f"Spool {spool_id} is archived. Please check the spoolman setup."
            await self._log_n_send(msg)
            return False

        # use the PATCH method on the spoolman api
        # get current printer hostname
        machine_hostname = self.printer_info["hostname"]
        logging.info(
            f"Setting spool {spool_info['filament']['name']} (id: {spool_info['id']}) for machine: {machine_hostname} @ slot {slot}")
        # get spool info from spoolman
        body = {
            "location": f"{machine_hostname}:{slot}",
        }
        args = {
            "request_method": "PATCH",
            "path": f"/v1/spool/{spool_id}",
            "body": body,
        }
        webrequest = WebRequest(
            endpoint=f"{self.spoolman_url}/spools/{spool_id}",
            args=args,
            request_type=RequestType.POST,
        )
        try:
            await self._proxy_spoolman_request(webrequest)
        except Exception as e:
            logging.error(
                f"Failed to set spool {spool_id} for machine {machine_hostname} @ slot {slot}: {e}")
            await self._log_n_send(f"Failed to set spool {spool_id} for machine {machine_hostname}")
            return False
        await self._log_n_send(f"Spool {spool_id} set for machine {machine_hostname} @ slot {slot}")
        await self.get_spools_for_machine(silent=True)
        if slot == 0 and (self.filament_slots == 1):
            await self.set_active_slot(slot)
            await self._log_n_send(f"{CONSOLE_TAB*2}Setting slot 0 as active (single slot machine)")
        return True

    async def unset_spool_slot(self, slot: int = 0) -> bool:
        '''
        Unsets the slot number for the current machine
        '''
        # get spools assigned to current machine
        if self.slot_occupation not in [False, None]:
            for spool in self.slot_occupation:
                if int(spool['location'].split(':')[1]) == slot:
                    logging.info(
                        f"Clearing slot {slot} for machine: {self.printer_info['hostname']}")
                    self.server.send_event(
                        "spoolman:clear_spool_slot", {"slot": slot}
                    )
                    await self.unset_spool_id(spool['id'])
                    await self._log_n_send(f"Slot {slot} cleared")
                    return True
            await self._log_n_send(f"No spool assigned to slot {slot}")
            return False
        else:
            msg = f"No spools found for this machine"
            await self._log_n_send(msg)
            return False

    async def clear_spool_slots(self) -> bool:
        '''
        Clears all slots for the current machine
        '''
        logging.info(
            f"Clearing spool slots for machine: {self.printer_info['hostname']}")
        self.server.send_event(
            "spoolman:clear_spool_slots", {}
        )
        # get spools assigned to current machine
        spools = self.slot_occupation
        if spools not in [False, None]:
            for spool in spools:
                # use the PATCH method on the spoolman api
                # get current printer hostname
                machine_hostname = self.printer_info["hostname"]
                logging.info(
                    f"Clearing spool {spool['id']} for machine: {machine_hostname}")
                # get spool info from spoolman
                __ = await self.get_info_for_spool(spool['id'])
                body = {
                    "location": "",
                }
                args = {
                    "request_method": "PATCH",
                    "path": f"/v1/spool/{spool['id']}",
                    "body": body,
                }
                webrequest = WebRequest(
                    endpoint=f"{self.spoolman_url}/spools/{spool['id']}",
                    args=args,
                    request_type=RequestType.POST,
                )
                try:
                    await self._proxy_spoolman_request(webrequest)
                except Exception as e:
                    logging.error(
                        f"Failed to clear spool {spool['id']} for machine {machine_hostname}: {e}")
                    await self._log_n_send(f"Failed to clear spool {spool['id']} for machine {machine_hostname}")
                    return False
                await self._log_n_send(f"Spool {spool['id']} cleared for machine {machine_hostname}")
                await self.get_spools_for_machine(silent=True)
            return True
        else:
            msg = f"No spools for machine {self.printer_info['hostname']}"
            await self._log_n_send(msg)
            return False

    async def check_filament(self):
        '''
        Uses metadata from the gcode to identify the filaments and runs some verifications
        based on the filament type and the amount of filament left in spoolman db.
        '''
        logging.info("Checking filaments")
        await self.get_spools_for_machine(silent=True)
        await self._log_n_send("Checking filament consistency: ")
        try:
            print_stats = await self.klippy_apis.query_objects({"print_stats": None})
        except asyncio.TimeoutError:
            # Klippy not connected
            logging.error("Could not retrieve print_stats through klippy API")
            self.server.send_event(
                "spoolman:check_failure", {
                    "message": "Could not retrieve print_stats through klippy API"}
            )
            return False
        # TODO: file path has to be better implemented like fetching it via klippy api ?
        filename = os.path.join('/home', os.getenv('USER'), 'printer_data',
                                'gcodes', print_stats["print_stats"]["filename"])
        state = print_stats["print_stats"]["state"]

        if state not in ['printing', 'paused']:
            # No print active
            msg = f"No print active, cannot get gcode from file (state: {state})"
            await self._log_n_send(msg)
            self.server.send_event(
                "spoolman:check_failure", {"message": msg}
            )
            return False

        # Get gcode from file
        if filename is None:
            logging.error("Filename is None")
            self.server.send_event(
                "spoolman:check_failure", {"message": "Filename is None"}
            )
            return False

        metadata: Dict[str, Any] = {}
        if not filename:
            logging.info("No filemame retrieved: {filename}")
            sys.exit(-1)
        try:
            metadata = extract_metadata(filename, False)
        except TimeoutError as e:
            raise TimeoutError(f"Failed to extract metadata from {filename}") from e

        # check that active spool is in machine's slots
        active_spool_id = await self._get_active_spool()
        if active_spool_id is None and self.filament_slots == 1:
            msg = f"No active spool set"
            await self._log_n_send(msg)
            self.server.send_event(
                "spoolman:check_failure", {"message": msg}
            )
            return False

        if self.slot_occupation == False:
            msg = "Failed to retrieve spools from spoolman"
            self.server.send_event(
                "spoolman:check_failure", {"message": msg}
            )
            return False
        found = False
        for spool in self.slot_occupation:
            if int(spool['id']) == active_spool_id:
                found = True
        if not found and self.filament_slots == 1:
            await self._log_n_send(f"Active spool {active_spool_id} is not assigned to this machine")
            await self._log_n_send("Run : ")
            await self._log_n_send(f"{CONSOLE_TAB}SET_SPOOL_SLOT ID={active_spool_id} SLOT=integer")
            return False

        if not self.slot_occupation:
            msg = f"No spools assigned to machine {self.printer_info['hostname']}"
            self.server.send_event(
                "spoolman:check_failure", {"message": msg}
            )
            return False

        mismatch, swap_table = await self.verify_consistency(metadata, self.slot_occupation)
        if mismatch != "critical":
            if mismatch == 'warning':
                msg1 = "FILAMENT MISMATCH(ES) BETWEEN SPOOLMAN AND SLICER DETECTED!"
                await self._log_n_send(mismatch.upper()+': '+msg1)
                msg2 = "Minor mismatches have been found, proceeding to print."
                await self._log_n_send(mismatch.upper()+': '+msg2)
            else:
                msg = "Slicer setup and spoolman db are consistent"
                await self._log_n_send(mismatch.upper()+': '+msg)
        else:
            msg1 = "FILAMENT MISMATCH(ES) BETWEEN SPOOLMAN AND SLICER DETECTED! PAUSING PRINT."
            await self._log_n_send(mismatch.upper()+': '+msg1)
            msg2 = "Please check the spoolman setup and physical spools to match the slicer setup."
            await self._log_n_send(mismatch.upper()+': '+msg2)
            # if printer is runnning, pause it
            self.server.send_event(
                "spoolman:check_failure", {"message": msg1+msg2}
            )
            if state != 'paused':
                await self.klippy_apis.pause_print()
            await self.klippy_apis.run_gcode("M300 P2000 S4000")
            return False

        # if swap table is not empty, prompt user for automatic tools swap
        if swap_table:
            msg = f"Swap table: {swap_table}"
            await self._log_n_send(msg)
            msg = "Dumping table to variables.cfg file in {}".format(os.path.join('/home', os.getenv('USER'), 'printer_data'))
            await self._log_n_send(msg)
            await self._gen_swap_table_cfg(swap_table)
        else :
            await self._gen_swap_table_cfg([None for __ in range(self.filament_slots)])

        return True

    async def _gen_swap_table_cfg(self, swap_table):
        '''
        Generates a swap table file in the specified save_variables file
        '''
        await self.klippy_apis.run_gcode("SAVE_VARIABLE VARIABLE=swap_table VALUE=\"{}\"".format(swap_table))

    async def __spool_info_notificator(self, web_request: WebRequest):
        '''
        Sends spool info when requested by a webrequest
        '''
        spool_id = await self._get_active_spool()
        return await self.get_info_for_spool(spool_id)

def load_component(config: ConfigHelper) -> SpoolManager:
    return SpoolManager(config)
