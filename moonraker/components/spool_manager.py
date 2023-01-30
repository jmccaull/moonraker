# Filament Manager for printer
#
# Copyright (C) 2021 Mateusz Brzezinski <mateusz.brzezinski@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from __future__ import annotations
import logging
import time
import math
from typing import TYPE_CHECKING, Dict, Any, List

from ..confighelper import ConfigHelper

if TYPE_CHECKING:
    from typing import Set, Optional
    from database import NamespaceWrapper
    from moonraker.websockets import WebRequest
    from .klippy_apis import KlippyAPI as APIComp

SPOOL_NAMESPACE = "spool_manager"
MOONRAKER_NAMESPACE = "moonraker"
ACTIVE_SPOOL_KEY = "spool_manager.active_spool_id"
MAX_SPOOLS = 1000


class Validation:
    def validate(self) -> Set[str]:
        failed = filter(lambda f: self.__getattribute__(f) is None,
                        self._required_attributes)
        return set(failed)

    _required_attributes: Set[str] = {"name"}


class Spool(Validation):
    _required_attributes: Set[str] = {'name', "diameter", "total_weight",
                                      'material'}

    def __init__(self, data={}):
        self.name: str = None
        self.color: str = None
        self.vendor: str = None
        self.material: str = None
        self.density: float = None
        self.diameter: float = None
        self.total_weight: float = None
        self.used_length: float = 0
        self.spool_weight: float = None
        self.first_used: float = None
        self.last_used: float = None
        self.cost: float = None
        self.comment: str = None

        self.update(data)

    def update(self, data):
        for a in data:
            if hasattr(self, a):
                setattr(self, a, data[a])

    def used_weight(self) -> float:
        used_weight = 0.0
        if self.diameter and self.density:
            r = self.diameter / 2
            density_mm = self.density / 1000
            used_weight = math.pi * r * r * self.used_length * density_mm
        return used_weight

    def serialize(self, include_calculated: bool = False):
        data = self.__dict__.copy()
        if include_calculated:
            data.update({'used_weight': self.used_weight()})
        return data


class SpoolManager:
    def __init__(self, config):
        self.server = config.get_server()

        self.materials = self._parse_materials_cfg(config)

        database = self.server.lookup_component("database")
        database.register_local_namespace(SPOOL_NAMESPACE)
        self.db: NamespaceWrapper = database.wrap_namespace(SPOOL_NAMESPACE,
                                                            parse_keys=False)
        self.moonraker_db: NamespaceWrapper = database.wrap_namespace(
            MOONRAKER_NAMESPACE, parse_keys=False)

        self.handler = SpoolManagerHandler(self.server, self)

    def on_exit(self):
        self.track_filament_usage()

    def _parse_materials_cfg(self, config: ConfigHelper) -> Dict[str, Dict[str, Any]]:
        template_names = config.get_prefix_sections('spool_manager template')
        logging.debug(template_names)
        configs = {name: config.getsection(name) for name in template_names}
        logging.debug(configs)
        converted: Dict[str, Dict[str, Any]] = {name: value.get_parsed_config()
                                                for name, value in configs}
        logging.debug(converted)
        return converted
        # materials_cfg = config.get('materials', '').strip()
        # lines = [line.strip().split(',') for
        #          line in materials_cfg.split('\n') if line.strip()]
        # return {f.strip(): {'density': float(d.strip())} for f, d in lines}

    async def find_spool(self, spool_id: str) -> Optional[Spool]:
        spool = await self.db.get(spool_id, None)

        if spool:
            return Spool(spool)
        else:
            return None

    async def set_active_spool(self, spool_id: str) -> bool:
        spool = await self.find_spool(spool_id)

        if spool:
            self.moonraker_db[ACTIVE_SPOOL_KEY] = spool_id
            self.server.send_event('spool_manager:active_spool_set',
                                   {'spool_id': spool_id})
            logging.info(f'Setting spool active, id: {spool_id}')
            return True
        else:
            return False

    async def get_active_spool_id(self) -> str:
        return await self.moonraker_db.get(ACTIVE_SPOOL_KEY, None)

    async def add_spool(self, data: Dict[str, Any]) -> str:
        if await self.db.length() >= MAX_SPOOLS:
            raise self.server.error(
                f"Reached maximum number of spools: {MAX_SPOOLS}", 400)
        if not data.get('density'):
            density = self.materials.get(data.get('material')).get('density') \
                if data.get('material') else None
            if not density:
                raise self.server.error(f'Density not provided and none found '
                                        f'for material: {data.get("material")}')
            data['density'] = density
        spool = Spool(data)
        missing_attrs = spool.validate()
        if missing_attrs:
            raise self.server.error(
                f"Missing spool attributes: {missing_attrs}", 400)

        next_spool_id = 0
        spools = await self.db.keys()
        if spools:
            next_spool_id = int(spools[-1], 16) + 1
        spool_id = f"{next_spool_id:06X}"

        self.db[spool_id] = spool.serialize()
        logging.info(f'New spool added, id: {spool_id}')

        return spool_id

    async def update_spool(self, spool_id: str, data: Dict[str, Any]) -> None:
        spool = await self.find_spool(spool_id)
        if spool:
            spool.update(data)
            missing_attrs = spool.validate()
            if missing_attrs:
                raise self.server.error(
                    f"Missing spool attributes: {missing_attrs}", 400)

            self.db[spool_id] = spool.serialize()
            logging.info(f'Spool id: {spool_id} updated.')

        return

    async def delete_spool(self, spool_id: str) -> None:
        await self.db.delete(spool_id)
        logging.info(f'Spool id: {spool_id} deleted.')
        self.server.send_event('spool_manager:spool_deleted',
                               {'spool_id': spool_id})
        return

    async def find_all_spools(self, show_inactive: bool) -> dict:
        spools = await self.db.items()
        spools = {k: Spool(v).serialize(include_calculated=True)
                  for k, v in spools
                  if show_inactive is True or v['active'] is True}

        return dict(spools)

    async def track_filament_usage(self):
        spool_id = await self.get_active_spool_id()
        spool = await self.find_spool(spool_id)

        if self.handler.extruded > 0:
            if not spool:
                logging.info("Active spool is not set, tracking ignored")
            else:
                used_length = self.handler.extruded

                old_used_length = spool.used_length
                old_used_weight = spool.used_weight()

                new_used_length = old_used_length + used_length
                spool.used_length = new_used_length

                new_used_weight = spool.used_weight()

                used_weight = new_used_weight - old_used_weight

                used_cost = 0
                if spool.cost and used_weight and spool.total_weight:
                    used_cost = used_weight / spool.total_weight * spool.cost

                if not spool.first_used:
                    spool.first_used = time.time()

                spool.last_used = time.time()

                await self.update_spool(spool_id, spool.serialize())

                metadata = {'spool_id': spool_id,
                            'used_weight': used_weight,
                            'cost': used_cost}

                self.server.send_event('spool_manager:filament_used', metadata)

                self.handler.extruded = 0

                logging.info(f'Tracking filament usage, '
                             f'spool_id: {spool_id}, ' +
                             f'length: {used_length}, ' +
                             f'old used_length: {old_used_length}, ' +
                             f'new used_length: {new_used_length} ' +
                             f'weight: {used_weight}, ' +
                             f'old used_weight: {old_used_weight}, ' +
                             f'new used_weight: {new_used_weight}, ' +
                             f'cost: {used_cost}')


class SpoolManagerHandler:
    def __init__(self, server, spool_manager: SpoolManager):
        self.spool_manager = spool_manager
        self.server = server
        self.highest_e_pos = 0.0
        self.extruded = 0.0

        self._register_listeners()
        self._register_endpoints()
        self.klippy_apis: APIComp = self.server.lookup_component('klippy_apis')

    def _register_listeners(self):
        self.server.register_event_handler('server:klippy_ready',
                                           self._handle_server_ready)

    def _register_endpoints(self):
        self.server.register_endpoint(
            "/spool_manager/spool", ['GET', 'POST', 'DELETE'],
            self._handle_spool_request)
        self.server.register_endpoint(
            "/spool_manager/spool/list", ['GET', 'POST', 'DELETE'],
            self._handle_spools_list)
        self.server.register_endpoint(
            "/spool_manager/spool/active", ['GET', 'POST'],
            self._handle_active_spool)
        self.server.register_endpoint(
            "/spool_manager/materials", ['GET'],
            self._handle_materials_list)

    async def _handle_server_ready(self):
        self.server.register_event_handler(
            'server:status_update', self._handle_status_update)
        sub: Dict[str, Optional[List[str]]] = {'toolhead': ['position']}
        logging.debug("sub: %s", sub)
        result = await self.klippy_apis.subscribe_objects(sub)
        logging.debug("result: %s", result)
        initial_e_pos = self._eposition_from_status(result)
        logging.debug("initial epos %s", initial_e_pos)
        if initial_e_pos is not None:
            self.highest_e_pos = initial_e_pos
            logging.info("Spool manager handler subscribed to epos")
        else:
            logging.error("Spool manager unable to subscribe to epos")
            raise self.server.error('Unable to subscribe to e position')

    def _eposition_from_status(self, status: Dict[str, Any]) -> Optional[float]:
        position = status.get('toolhead', {}).get('position', [])
        return position[3] if len(position) > 0 else None

    def _handle_status_update(self, status: Dict[str, Any]) -> None:
        epos = self._eposition_from_status(status)
        if epos and epos > self.highest_e_pos:
            self.extruded += epos - self.highest_e_pos
            self.highest_e_pos = epos
            logging.debug("extuded: %s, highest_epos: %s",
                          self.extruded, self.highest_e_pos)

    async def _handle_spool_request(self, web_request: WebRequest):
        await self.spool_manager.track_filament_usage()
        action = web_request.get_action()

        if action == 'GET':
            spool_id = web_request.get_str('id')
            spool = await self.spool_manager.find_spool(spool_id)
            if spool:
                return {'spool': spool.serialize(include_calculated=True)}
            else:
                return None
        elif action == 'POST':
            return await self._update_single_spool(web_request.args)
        elif action == 'DELETE':
            return await self._delete_single_spool(web_request.get_str('id'))

    async def _delete_single_spool(self, spool_id: str):
        await self.spool_manager.delete_spool(spool_id)
        return 'OK'

    async def _update_single_spool(self, data: Dict[str: Any]):
        spool_id = data.get('id', None)
        logging.debug("initial id check %s", spool_id)
        if spool_id:
            await self.spool_manager.update_spool(spool_id, data)
        else:
            spool_id = await self.spool_manager.add_spool(data)
            logging.debug("adding spool %s", spool_id)
        return spool_id

    async def _handle_spools_list(self, web_request: WebRequest):
        await self.spool_manager.track_filament_usage()
        action = web_request.get_action()
        if action == 'GET':
            show_inactive = web_request.get_boolean('show_inactive', False)
            spools = await self.spool_manager.find_all_spools(show_inactive)
            return {'spools': spools}
        elif action == 'POST':
            spools: [Dict[str, Any]] = web_request.get('spools')
            return {'spools': await self._update_single_spool(spool)
                    for spool in spools}
        elif action == 'DELETE':
            ids: [str] = web_request.get('ids')
            return [await self._delete_single_spool(id) for id in ids]

    async def _handle_active_spool(self, web_request: WebRequest):
        await self.spool_manager.track_filament_usage()
        action = web_request.get_action()

        if action == 'GET':
            spool_id = await self.spool_manager.get_active_spool_id()
            return {"spool_id": spool_id}
        elif action == 'POST':
            spool_id = web_request.get_str('id')
            if await self.spool_manager.set_active_spool(spool_id):
                return 'OK'
            else:
                raise self.server.error(
                    f"Spool id {spool_id} not found", 404)

    async def _handle_materials_list(self, web_request: WebRequest):
        return {'materials': self.spool_manager.materials}


def load_component(config):
    return SpoolManager(config)
