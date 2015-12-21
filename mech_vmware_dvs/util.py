# Copyright 2015 Mirantis, Inc.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re
from time import sleep
import uuid

import six
from oslo_log import log
from oslo.vmware import api
from oslo.vmware import exceptions as vmware_exceptions
from oslo.vmware import vim_util

from neutron.i18n import _LI, _LW, _

from mech_vmware_dvs import exceptions
from mech_vmware_dvs import security_group_utils as sg_utils

LOG = log.getLogger(__name__)


AGENT_TYPE_DVS = 'DVS agent'
# protocol number according to RFC 1700
PROTOCOL = {'icmp': 1,
            'tcp': 6,
            'udp': 17}

DVS_PORTGROUP_NAME_MAXLEN = 80

LOGIN_RETRIES = 3

VM_NETWORK_DEVICE_TYPES = [
    'VirtualE1000', 'VirtualE1000e', 'VirtualPCNet32',
    'VirtualSriovEthernetCard', 'VirtualVmxnet']

CONCURRENT_MODIFICATION_TEXT = 'Cannot complete operation due to concurrent '\
                               'modification by another operation.'

LOGIN_PROBLEM_TEXT = "Cannot complete login due to an incorrect "\
                     "user name or password"

DELETED_TEXT = "The object has already been deleted or has not been "\
               "completely created"


class DVSController(object):
    """Controls one DVS."""

    def __init__(self, dvs_name, connection):
        self.connection = connection
        try:
            self._datacenter = self._get_datacenter(connection)
            self._dvs = self._get_dvs(dvs_name, connection, self._datacenter)
        except vmware_exceptions.VimException as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    def create_network(self, network, segment):
        name = self._get_net_name(network)
        blocked = not network['admin_state_up']

        try:
            pg_spec = self._build_pg_create_spec(
                name,
                segment['segmentation_id'],
                blocked)
            pg_create_task = self.connection.invoke_api(
                self.connection.vim,
                'CreateDVPortgroup_Task',
                self._dvs, spec=pg_spec)

            result = self.connection.wait_for_task(pg_create_task)
        except vmware_exceptions.VimException as e:
            raise exceptions.wrap_wmvare_vim_exception(e)
        else:
            pg = result.result
            LOG.info(_LI('Network %(name)s created \n%(pg_ref)s'),
                     {'name': name, 'pg_ref': pg})

    def update_network(self, network, original=None):
        if original:
            name = self._get_net_name(original)
        else:
            name = self._get_net_name(network)
        blocked = not network['admin_state_up']
        try:
            pg_ref = self._get_pg_by_name(name)
            pg_config_info = self._get_config_by_ref(pg_ref)

            if (pg_config_info.defaultPortConfig.blocked.value != blocked or
                    (original and original['name'] != network['name'])):
                # we upgrade only defaultPortConfig, because it is inherited
                # by all ports in PortGroup, unless they are explicite
                # overwritten on specific port.
                pg_spec = self._build_pg_update_spec(
                    pg_config_info.configVersion,
                    blocked=blocked)
                pg_spec.name = self._get_net_name(network)
                pg_update_task = self.connection.invoke_api(
                    self.connection.vim,
                    'ReconfigureDVPortgroup_Task',
                    pg_ref, spec=pg_spec)

                self.connection.wait_for_task(pg_update_task)
                LOG.info(_LI('Network %(name)s updated'), {'name': name})
        except vmware_exceptions.VimException as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    def delete_network(self, network):
        name = self._get_net_name(network)
        while True:
            try:
                pg_ref = self._get_pg_by_name(name)
                pg_delete_task = self.connection.invoke_api(
                    self.connection.vim,
                    'Destroy_Task',
                    pg_ref)
                self.connection.wait_for_task(pg_delete_task)
                LOG.info(_LI('Network %(name)s deleted.') % {'name': name})
                break
            except exceptions.PortGroupNotFound:
                LOG.debug('Network %s not present in vcenter.' % name)
                break
            except vmware_exceptions.VimException as e:
                raise exceptions.wrap_wmvare_vim_exception(e)
            except vmware_exceptions.VMwareDriverException as e:
                if DELETED_TEXT in e.message:
                    sleep(1)
                else:
                    raise

    def switch_port_blocked_state(self, port):
        state = not port['admin_state_up']

        port_info = self._get_port_info_by_name(port['id'])

        builder = SpecBuilder(self.connection.vim.client.factory)
        port_settings = builder.port_setting()
        port_settings.blocked = builder.blocked(state)

        update_spec = builder.port_config_spec(
            port_info.config.configVersion, port_settings)
        update_spec.key = port_info.key
        update_task = self.connection.invoke_api(
            self.connection.vim, 'ReconfigureDVPort_Task',
            self._dvs, port=[update_spec])
        self.connection.wait_for_task(update_task)

    def book_port(self, network, port_name):
        try:
            net_name = self._get_net_name(network)
            pg = self._get_pg_by_name(net_name)
            while True:
                try:
                    port_info = self._lookup_unbound_port(pg)
                    break
                except exceptions.UnboundPortNotFound:
                    try:
                        self._increase_ports_on_portgroup(pg)
                    except (vmware_exceptions.VMwareDriverException,
                            exceptions.VMWareDVSException) as e:
                        if CONCURRENT_MODIFICATION_TEXT in e.message:
                            LOG.info(_LI('Concurent modification on '
                                         'increase port group.'))
                            continue
            builder = SpecBuilder(self.connection.vim.client.factory)
            port_settings = builder.port_setting()
            port_settings.blocked = builder.blocked(False)
            update_spec = builder.port_config_spec(
                port_info.config.configVersion, port_settings, name=port_name)
            update_spec.key = port_info.key
            update_task = self.connection.invoke_api(
                self.connection.vim, 'ReconfigureDVPort_Task',
                self._dvs, port=[update_spec])
            self.connection.wait_for_task(update_task)
        except vmware_exceptions.VimException as e:
            raise exceptions.wrap_wmvare_vim_exception(e)

    def release_port(self, port):
        port_info = self._get_port_info_by_name(port['id'])
        builder = SpecBuilder(self.connection.vim.client.factory)
        update_spec = builder.port_config_spec(
            port_info.config.configVersion, name='')
        update_spec.key = port_info.key
        update_task = self.connection.invoke_api(
            self.connection.vim, 'ReconfigureDVPort_Task',
            self._dvs, port=[update_spec])
        self.connection.wait_for_task(update_task)

    def _build_pg_create_spec(self, name, vlan_tag, blocked):
        builder = SpecBuilder(self.connection.vim.client.factory)
        port_setting = builder.port_setting()

        port_setting.vlan = builder.vlan(vlan_tag)
        port_setting.blocked = builder.blocked(blocked)

        port_setting.filterPolicy = builder.filter_policy([])

        pg = builder.pg_config(port_setting)
        pg.name = name
        pg.numPorts = 0

        # Equivalent of vCenter static binding type.
        pg.type = 'earlyBinding'
        pg.description = 'Managed By Neutron'
        return pg

    def _build_pg_update_spec(self, config_version,
                              blocked=None,
                              ports_number=None):
        builder = SpecBuilder(self.connection.vim.client.factory)
        port = builder.port_setting()
        if blocked is not None:
            port.blocked = builder.blocked(blocked)
        pg = builder.pg_config(port)
        if ports_number:
            pg.numPorts = ports_number
        pg.configVersion = config_version
        return pg

    def _get_datacenter(self, connection):
        """Get the datacenter reference."""
        # FIXME(dobgun): lookup datacenter by name(add it into config)
        results = connection.invoke_api(
            vim_util, 'get_objects', connection.vim,
            'Datacenter', 100, ['name'])
        return results.objects[0].obj

    def _get_dvs(self, dvs_name, connection, datacenter):
        """Get the dvs by name"""
        network_folder = connection.invoke_api(
            vim_util, 'get_object_property', connection.vim,
            datacenter, 'networkFolder')
        results = connection.invoke_api(
            vim_util, 'get_object_property', connection.vim,
            network_folder, 'childEntity')
        networks = results.ManagedObjectReference
        dvswitches = self._get_object_by_type(networks,
                                              'VmwareDistributedVirtualSwitch')
        for dvs in dvswitches:
            name = connection.invoke_api(
                vim_util, 'get_object_property',
                connection.vim, dvs, 'name')
            if name == dvs_name:
                dvs_ref = dvs
                break
        else:
            raise exceptions.DVSNotFound(dvs_name=dvs_name)
        return dvs_ref

    def _get_pg_by_name(self, pg_name):
        """Get the dpg ref by name"""
        net_list = self.connection.invoke_api(
            vim_util, 'get_object_property', self.connection.vim,
            self._datacenter, 'network').ManagedObjectReference
        type_value = 'DistributedVirtualPortgroup'
        pg_list = self._get_object_by_type(net_list, type_value)
        for pg in pg_list:
            name = self.connection.invoke_api(
                vim_util, 'get_object_property',
                self.connection.vim, pg, 'name')
            if pg_name == name:
                return pg
        raise exceptions.PortGroupNotFound(pg_name=pg_name)

    def _get_config_by_ref(self, ref):
        """pg - ManagedObjectReference of Port Group"""
        return self.connection.invoke_api(
            vim_util, 'get_object_property',
            self.connection.vim, ref, 'config')

    @staticmethod
    def _get_net_name(network):
        # TODO(dbogun): check network['bridge'] generation algorithm our
        # must match it
        suffix = network['id']

        name = network.get('name')
        if not name:
            return suffix

        suffix = '-' + suffix
        if DVS_PORTGROUP_NAME_MAXLEN < len(name) + len(suffix):
            raise exceptions.InvalidNetworkName(
                name=name,
                reason=_('name length %(length)s, while allowed length is '
                         '%(max_length)d') % {
                    'length': len(name),
                    'max_length': DVS_PORTGROUP_NAME_MAXLEN - len(suffix)})

        if not re.match(r'^[\w-]+$', name):
            raise exceptions.InvalidNetworkName(
                name=name,
                reason=_('name contains illegal symbols. Only alphanumeric, '
                         'underscore and hyphen are allowed.'))

        return name + suffix

    @staticmethod
    def _get_object_by_type(results, type_value):
        """Get object by type.

        Get the desired object from the given objects
        result by the given type.
        """
        return [obj for obj in results
                if obj._type == type_value]

    def _get_ports_for_pg(self, pg_name):
        pg = self._get_pg_by_name(pg_name)
        return self.connection.invoke_api(
            vim_util, 'get_object_property',
            self.connection.vim, pg, 'portKeys')[0]

    def _lookup_unbound_port(self, port_group):
        builder = SpecBuilder(self.connection.vim.client.factory)
        criteria = builder.port_criteria(port_group_key=port_group.value)

        ports = self.connection.invoke_api(
            self.connection.vim,
            'FetchDVPorts',
            self._dvs, criteria=criteria)
        for port in ports:
            if not getattr(port.config, 'name', None):
                return port
        raise exceptions.UnboundPortNotFound()

    def _increase_ports_on_portgroup(self, port_group):
        pg_info = self._get_config_by_ref(port_group)
        ports_number = pg_info.numPorts * 2 if pg_info.numPorts else 1
        pg_spec = self._build_pg_update_spec(
            pg_info.configVersion, ports_number=ports_number)
        pg_update_task = self.connection.invoke_api(
            self.connection.vim,
            'ReconfigureDVPortgroup_Task',
            port_group, spec=pg_spec)
        self.connection.wait_for_task(pg_update_task)

    def _get_port_info_by_portkey(self, port_key):
        """pg - ManagedObjectReference of Port Group"""
        builder = SpecBuilder(self.connection.vim.client.factory)
        criteria = builder.port_criteria(port_key=port_key)
        return self.connection.invoke_api(
            self.connection.vim,
            'FetchDVPorts',
            self._dvs, criteria=criteria)[0]

    def _get_port_info_by_name(self, name, port_list=None):
        if port_list is None:
            port_list = self._get_ports()
        ports = [port for port in port_list
                 if port.config.name == name]
        if not ports:
            raise exceptions.PortNotFound(id=name)
        else:
            if len(ports) > 1:
                LOG.warn(_LW("Multiple ports found for name %s."), name)
        return ports[0]

    def _get_ports(self):
        ports = []
        net_list = self.connection.invoke_api(
            vim_util, 'get_object_property', self.connection.vim,
            self._datacenter, 'network').ManagedObjectReference
        type_value = 'DistributedVirtualPortgroup'
        pg_list = self._get_object_by_type(net_list, type_value)
        port_keys = []
        for pg in pg_list:
            pk = self.connection.invoke_api(vim_util,
                                            'get_object_property',
                                            self.connection.vim, pg,
                                            'portKeys')
            if not isinstance(pk, basestring):
                port_keys = port_keys + pk[0]

        for port_key in port_keys:
            port = self._get_port_info_by_portkey(port_key)
            if (getattr(port.config, 'name', None) is not None
                    and self._valid_uuid(port.config.name)):
                ports.append(port)
        return ports

    def _get_ports_ids(self):
        return [port.config.name for port in self._get_ports()]

    def _valid_uuid(self, name):
        try:
            uuid.UUID(name, version=4)
        except ValueError:
            return False
        return True


class SpecBuilder(object):
    """Builds specs for vSphere API calls"""

    def __init__(self, spec_factory):
        self.factory = spec_factory

    def pg_config(self, default_port_config):
        spec = self.factory.create('ns0:DVPortgroupConfigSpec')
        spec.defaultPortConfig = default_port_config
        policy = self.factory.create('ns0:DVPortgroupPolicy')
        policy.blockOverrideAllowed = '1'
        policy.livePortMovingAllowed = '0'
        policy.portConfigResetAtDisconnect = '1'
        policy.shapingOverrideAllowed = '0'
        policy.trafficFilterOverrideAllowed = '1'
        policy.vendorConfigOverrideAllowed = '0'
        spec.policy = policy
        return spec

    def port_config_spec(self, version, setting=None, name=None):
        spec = self.factory.create('ns0:DVPortConfigSpec')
        spec.configVersion = version
        spec.operation = 'edit'
        if setting:
            spec.setting = setting

        if name is not None:
            spec.name = name
        return spec

    def port_lookup_criteria(self):
        return self.factory.create('ns0:DistributedVirtualSwitchPortCriteria')

    def port_setting(self):
        return self.factory.create('ns0:VMwareDVSPortSetting')

    def filter_policy(self, rules):
        filter_policy = self.factory.create('ns0:DvsFilterPolicy')
        if rules:
            traffic_ruleset = self.factory.create('ns0:DvsTrafficRuleset')
            traffic_ruleset.enabled = '1'
            traffic_ruleset.rules = rules
            filter_config = self.factory.create('ns0:DvsTrafficFilterConfig')
            filter_config.agentName = "dvfilter-generic-vmware"
            filter_config.inherited = '0'
            filter_config.trafficRuleset = traffic_ruleset
            filter_policy.filterConfig = [filter_config]
            filter_policy.inherited = '0'
        else:
            filter_policy.inherited = '1'
        return filter_policy

    def port_config(self, port_key, sg_rules):
        rules = []
        reversed_rules = []
        seq = 0
        for rule_info in sg_rules:
            if 'ip_set' in rule_info:
                for ip in rule_info['ip_set']:
                    rule = self._create_rule(rule_info, ip,
                                             name='remote security group')
                    rules.append(rule.build(seq))
                    seq += 10
                    reversed_rules.append(rule.reverse())
            else:
                rule = self._create_rule(rule_info, name='regural')
                rules.append(rule.build(seq))
                seq += 10
                reversed_rules.append(rule.reverse())

        for r in reversed_rules:
            rules.append(r.build(seq))
            seq += 10

        for i, protocol in enumerate(PROTOCOL.values()):
            rules.append(
                sg_utils.DropAllRule(self.factory, None, protocol,
                                     name='drop all').build(seq + i * 10))

        filter_policy = self.filter_policy(rules)
        setting = self.port_setting()
        setting.filterPolicy = filter_policy

        spec = self.factory.create('ns0:DVPortConfigSpec')
        spec.operation = 'edit'
        spec.setting = setting
        spec.key = port_key
        return spec

    def _create_rule(self, rule_info, ip=None, name=None):
        if rule_info['direction'] == 'ingress':
            rule_class = sg_utils.IngressRule
            cidr = rule_info.get('source_ip_prefix')
        else:
            rule_class = sg_utils.EgressRule
            cidr = rule_info.get('dest_ip_prefix')
        rule = rule_class(
            spec_factory=self.factory,
            ethertype=rule_info['ethertype'],
            protocol=rule_info.get('protocol'),
            name=name
        )
        rule.cidr = ip or cidr

        if rule_info.get('protocol') in ('tcp', 'udp'):
            rule.port_range = (rule_info.get('port_range_min'),
                               rule_info.get('port_range_max'))
            rule.backward_port_range = (
                rule_info.get('source_port_range_min') or 32768,
                rule_info.get('source_port_range_max') or 65535)
        return rule

    def port_criteria(self, port_key=None, port_group_key=None):
        criteria = self.factory.create(
            'ns0:DistributedVirtualSwitchPortCriteria')
        if port_key:
            criteria.portKey = port_key
        if port_group_key:
            criteria.portgroupKey = port_group_key
            criteria.inside = '1'
        return criteria

    def vlan(self, vlan_tag):
        spec_ns = 'ns0:VmwareDistributedVirtualSwitchVlanIdSpec'
        spec = self.factory.create(spec_ns)
        spec.inherited = '0'
        spec.vlanId = vlan_tag
        return spec

    def blocked(self, value):
        """Value should be True or False"""
        spec = self.factory.create('ns0:BoolPolicy')
        if value:
            spec.inherited = '0'
            spec.value = 'true'
        else:
            spec.inherited = '1'
            spec.value = 'false'
        return spec


def create_network_map_from_config(config):
    """Creates physical network to dvs map from config"""
    connection = api.VMwareAPISession(
        config.vsphere_hostname,
        config.vsphere_login,
        config.vsphere_password,
        config.api_retry_count,
        config.task_poll_interval)
    network_map = {}
    for pair in config.network_maps:
        network, dvs = pair.split(':')
        network_map[network] = DVSController(dvs, connection)
    return network_map


def create_port_map(dvs_list):
    port_map = {}
    for dvs in dvs_list:
        port_map[dvs] = dvs._get_ports_ids()

    return port_map


def wrap_retry(func):
    """
    Retry operation on dvs when concurrent modification by another operation
    was discovered
    """
    @six.wraps(func)
    def wrapper(*args, **kwargs):
        login_failures = 0
        while True:
            try:
                return func(*args, **kwargs)
            except (vmware_exceptions.VMwareDriverException,
                    exceptions.VMWareDVSException) as e:
                if CONCURRENT_MODIFICATION_TEXT in e.message:
                    continue
                elif (LOGIN_PROBLEM_TEXT in getattr(e, 'msg', '')
                        and login_failures < LOGIN_RETRIES - 1):
                    login_failures += 1
                    continue
                else:
                    raise
    return wrapper
