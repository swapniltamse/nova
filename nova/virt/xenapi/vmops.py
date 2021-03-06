# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Citrix Systems, Inc.
# Copyright 2010 OpenStack LLC.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Management class for VM-related functions (spawn, reboot, etc).
"""

import base64
import json
import pickle
import random
import sys
import time
import uuid

from eventlet import greenthread
import M2Crypto

from nova.compute import api as compute
from nova.compute import power_state
from nova import context as nova_context
from nova import db
from nova import exception
from nova import flags
from nova import log as logging
from nova import utils
from nova.virt import driver
from nova.virt.xenapi import volume_utils
from nova.virt.xenapi import network_utils
from nova.virt.xenapi import vm_utils

VolumeHelper = volume_utils.VolumeHelper
NetworkHelper = network_utils.NetworkHelper
VMHelper = vm_utils.VMHelper
XenAPI = None
LOG = logging.getLogger("nova.virt.xenapi.vmops")

FLAGS = flags.FLAGS
flags.DEFINE_integer('agent_version_timeout', 300,
                     'number of seconds to wait for agent to be fully '
                     'operational')
flags.DEFINE_integer('xenapi_running_timeout', 60,
                     'number of seconds to wait for instance to go to '
                     'running state')
flags.DEFINE_string('xenapi_vif_driver',
                    'nova.virt.xenapi.vif.XenAPIBridgeDriver',
                    'The XenAPI VIF driver using XenServer Network APIs.')
flags.DEFINE_bool('xenapi_generate_swap',
                  False,
                  'Whether to generate swap (False means fetching it'
                  ' from OVA)')


RESIZE_TOTAL_STEPS = 5
BUILD_TOTAL_STEPS = 4


def cmp_version(a, b):
    """Compare two version strings (eg 0.0.1.10 > 0.0.1.9)"""
    a = a.split('.')
    b = b.split('.')

    # Compare each individual portion of both version strings
    for va, vb in zip(a, b):
        ret = int(va) - int(vb)
        if ret:
            return ret

    # Fallback to comparing length last
    return len(a) - len(b)


class VMOps(object):
    """
    Management class for VM-related tasks
    """
    def __init__(self, session, product_version):
        self.XenAPI = session.get_imported_xenapi()
        self.compute_api = compute.API()
        self._session = session
        self.poll_rescue_last_ran = None
        VMHelper.XenAPI = self.XenAPI
        vif_impl = utils.import_class(FLAGS.xenapi_vif_driver)
        self.vif_driver = vif_impl(xenapi_session=self._session)
        self._product_version = product_version

    def list_instances(self):
        """List VM instances."""
        # TODO(justinsb): Should we just always use the details method?
        #  Seems to be the same number of API calls..
        name_labels = []
        for vm_ref, vm_rec in VMHelper.list_vms(self._session):
            name_labels.append(vm_rec["name_label"])

        return name_labels

    def list_instances_detail(self):
        """List VM instances, returning InstanceInfo objects."""
        details = []
        for vm_ref, vm_rec in VMHelper.list_vms(self._session):
            name = vm_rec["name_label"]

            # TODO(justinsb): This a roundabout way to map the state
            openstack_format = VMHelper.compile_info(vm_rec)
            state = openstack_format['state']

            instance_info = driver.InstanceInfo(name, state)
            details.append(instance_info)

        return details

    def confirm_migration(self, migration, instance, network_info):
        name_label = self._get_orig_vm_name_label(instance)
        vm_ref = VMHelper.lookup(self._session, name_label)
        return self._destroy(instance, vm_ref, network_info, shutdown=False)

    def finish_revert_migration(self, instance):
        # NOTE(sirp): the original vm was suffixed with '-orig'; find it using
        # the old suffix, remove the suffix, then power it back on.
        name_label = self._get_orig_vm_name_label(instance)
        vm_ref = VMHelper.lookup(self._session, name_label)

        # Remove the '-orig' suffix (which was added in case the resized VM
        # ends up on the source host, common during testing)
        name_label = instance.name
        VMHelper.set_vm_name_label(self._session, vm_ref, name_label)

        self._start(instance, vm_ref)

    def finish_migration(self, context, migration, instance, disk_info,
                         network_info, image_meta, resize_instance):
        vdi_uuid = self._move_disks(instance, disk_info)

        if resize_instance:
            self._resize_instance(instance, vdi_uuid)

        vm_ref = self._create_vm(context, instance,
                                 [dict(vdi_type='os', vdi_uuid=vdi_uuid)],
                                 network_info, image_meta)

        # 5. Start VM
        self._start(instance, vm_ref=vm_ref)
        self._update_instance_progress(context, instance,
                                       step=5,
                                       total_steps=RESIZE_TOTAL_STEPS)

    def _start(self, instance, vm_ref=None):
        """Power on a VM instance"""
        if not vm_ref:
            vm_ref = VMHelper.lookup(self._session, instance.name)
        if vm_ref is None:
            raise Exception(_('Attempted to power on non-existent instance'
            ' bad instance id %s') % instance.id)
        LOG.debug(_("Starting instance %s"), instance.name)
        self._session.call_xenapi('VM.start', vm_ref, False, False)

    def _create_disks(self, context, instance, image_meta):
        disk_image_type = VMHelper.determine_disk_image_type(image_meta)
        vdis = VMHelper.fetch_image(context, self._session,
                instance, instance.image_ref,
                instance.user_id, instance.project_id,
                disk_image_type)

        for vdi in vdis:
            if vdi["vdi_type"] == "os":
                self._resize_instance(instance, vdi["vdi_uuid"])

        return vdis

    def spawn(self, context, instance, image_meta, network_info):
        vdis = None
        try:
            # 1. Vanity Step
            # NOTE(sirp): _create_disk will potentially take a *very* long
            # time to complete since it has to fetch the image over the
            # network and images can be several gigs in size. To avoid
            # progress remaining at 0% for too long, which will appear to be
            # an error, we insert a "vanity" step to bump the progress up one
            # notch above 0.
            self._update_instance_progress(context, instance,
                                           step=1,
                                           total_steps=BUILD_TOTAL_STEPS)

            # 2. Fetch the Image over the Network
            vdis = self._create_disks(context, instance, image_meta)
            self._update_instance_progress(context, instance,
                                           step=2,
                                           total_steps=BUILD_TOTAL_STEPS)

            # 3. Create the VM records
            vm_ref = self._create_vm(context, instance, vdis, network_info,
                                     image_meta)
            self._update_instance_progress(context, instance,
                                           step=3,
                                           total_steps=BUILD_TOTAL_STEPS)

            # 4. Boot the Instance
            self._spawn(instance, vm_ref)
            self._update_instance_progress(context, instance,
                                           step=4,
                                           total_steps=BUILD_TOTAL_STEPS)

        except (self.XenAPI.Failure, OSError, IOError) as spawn_error:
            LOG.exception(_("instance %s: Failed to spawn"),
                          instance.id, exc_info=sys.exc_info())
            LOG.debug(_('Instance %s failed to spawn - performing clean-up'),
                      instance.id)
            self._handle_spawn_error(vdis, spawn_error)
            raise spawn_error

    def spawn_rescue(self, context, instance, image_meta, network_info):
        """Spawn a rescue instance."""
        self.spawn(context, instance, image_meta, network_info)

    def _create_vm(self, context, instance, vdis, network_info, image_meta):
        """Create VM instance."""
        instance_name = instance.name
        vm_ref = VMHelper.lookup(self._session, instance_name)
        if vm_ref is not None:
            raise exception.InstanceExists(name=instance_name)

        # Ensure enough free memory is available
        if not VMHelper.ensure_free_mem(self._session, instance):
            raise exception.InsufficientFreeMemory(uuid=instance.uuid)

        disk_image_type = VMHelper.determine_disk_image_type(image_meta)
        kernel = None
        ramdisk = None
        try:
            if instance.kernel_id:
                kernel = VMHelper.fetch_image(context, self._session,
                        instance, instance.kernel_id, instance.user_id,
                        instance.project_id, vm_utils.ImageType.KERNEL)[0]
            if instance.ramdisk_id:
                ramdisk = VMHelper.fetch_image(context, self._session,
                        instance, instance.ramdisk_id, instance.user_id,
                        instance.project_id, vm_utils.ImageType.RAMDISK)[0]

            # NOTE(jk0): Since vdi_type may contain either 'os' or 'swap', we
            # need to ensure that the 'swap' VDI is not chosen as the mount
            # point for file injection.
            first_vdi_ref = None
            for vdi in vdis:
                if vdi.get('vdi_type') != 'swap':
                    # Create the VM ref and attach the first disk
                    first_vdi_ref = self._session.call_xenapi(
                            'VDI.get_by_uuid', vdi['vdi_uuid'])

            vm_mode = instance.vm_mode and instance.vm_mode.lower()
            if vm_mode == 'pv':
                use_pv_kernel = True
            elif vm_mode in ('hv', 'hvm'):
                use_pv_kernel = False
                vm_mode = 'hvm'  # Normalize
            else:
                use_pv_kernel = VMHelper.determine_is_pv(self._session,
                        instance.id, first_vdi_ref, disk_image_type,
                        instance.os_type)
                vm_mode = use_pv_kernel and 'pv' or 'hvm'

            if instance.vm_mode != vm_mode:
                # Update database with normalized (or determined) value
                db.instance_update(nova_context.get_admin_context(),
                                   instance['id'], {'vm_mode': vm_mode})
            vm_ref = VMHelper.create_vm(self._session, instance,
                    kernel and kernel.get('file', None) or None,
                    ramdisk and ramdisk.get('file', None) or None,
                    use_pv_kernel)
        except (self.XenAPI.Failure, OSError, IOError) as vm_create_error:
            # Collect VDI/file resources to clean up;
            # These resources will be removed by _handle_spawn_error.
            LOG.exception(_("instance %s: Failed to spawn - " +
                            "Unable to create VM"),
                          instance.id, exc_info=sys.exc_info())
            last_arg = None
            resources = []

            if vm_create_error.args:
                last_arg = vm_create_error.args[-1]
            if isinstance(last_arg, list):
                resources = last_arg
            else:
                vm_create_error.args = vm_create_error.args + (resources,)

            if kernel:
                resources.append(kernel)
            if ramdisk:
                resources.append(ramdisk)

            raise vm_create_error

        # Add disks to VM
        self._attach_disks(instance, disk_image_type, vm_ref, first_vdi_ref,
            vdis)

        # Alter the image before VM start for network injection.
        if FLAGS.flat_injected:
            VMHelper.preconfigure_instance(self._session, instance,
                                           first_vdi_ref, network_info)

        self.create_vifs(vm_ref, instance, network_info)
        self.inject_network_info(instance, network_info, vm_ref)
        self.inject_hostname(instance, vm_ref, instance['hostname'])

        return vm_ref

    def _attach_disks(self, instance, disk_image_type, vm_ref, first_vdi_ref,
            vdis):
        ctx = nova_context.get_admin_context()

        instance_uuid = instance['uuid']

        # device 0 reserved for RW disk
        userdevice = 0

        # DISK_ISO needs two VBDs: the ISO disk and a blank RW disk
        if disk_image_type == vm_utils.ImageType.DISK_ISO:
            LOG.debug("detected ISO image type, going to create blank VM for "
                  "install")

            cd_vdi_ref = first_vdi_ref
            first_vdi_ref = VMHelper.fetch_blank_disk(session=self._session,
                        instance_type_id=instance.instance_type_id)

            VolumeHelper.create_vbd(session=self._session, vm_ref=vm_ref,
                vdi_ref=first_vdi_ref, userdevice=userdevice, bootable=False)

            # device 1 reserved for rescue disk and we've used '0'
            userdevice = 2
            VMHelper.create_cd_vbd(session=self._session, vm_ref=vm_ref,
                    vdi_ref=cd_vdi_ref, userdevice=userdevice, bootable=True)

            # set user device to next free value
            userdevice += 1
        else:
            if instance.auto_disk_config:
                LOG.debug(_("Auto configuring disk for instance"
                            " %(instance_uuid)s, attempting to"
                            " resize partition...") % locals())
                instance_type = db.instance_type_get(ctx,
                        instance.instance_type_id)
                VMHelper.auto_configure_disk(session=self._session,
                                             vdi_ref=first_vdi_ref,
                                             new_gb=instance_type['local_gb'])

            VolumeHelper.create_vbd(session=self._session, vm_ref=vm_ref,
                                    vdi_ref=first_vdi_ref,
                                    userdevice=userdevice, bootable=True)

            # set user device to next free value
            # userdevice 1 is reserved for rescue and we've used '0'
            userdevice = 2

        instance_type = db.instance_type_get(ctx, instance.instance_type_id)
        swap_mb = instance_type['swap']
        generate_swap = swap_mb and FLAGS.xenapi_generate_swap
        if generate_swap:
            VMHelper.generate_swap(session=self._session, instance=instance,
                                   vm_ref=vm_ref, userdevice=userdevice,
                                   swap_mb=swap_mb)
            userdevice += 1

        # Attach any other disks
        for vdi in vdis[1:]:
            if generate_swap and vdi['vdi_type'] == 'swap':
                continue
            vdi_ref = self._session.call_xenapi('VDI.get_by_uuid',
                    vdi['vdi_uuid'])
            VolumeHelper.create_vbd(session=self._session, vm_ref=vm_ref,
                    vdi_ref=vdi_ref, userdevice=userdevice,
                    bootable=False)
            userdevice += 1

    def _configure_instance(self, ctx, instance, vm_ref):
        # Inject files, if necessary
        injected_files = instance.injected_files
        if injected_files:
            # Check if this is a JSON-encoded string and convert if needed.
            if isinstance(injected_files, basestring):
                try:
                    injected_files = json.loads(injected_files)
                except ValueError:
                    LOG.exception(
                        _("Invalid value for injected_files: '%s'")
                            % injected_files)
                    injected_files = []
            # Inject any files, if specified
            for path, contents in instance.injected_files:
                LOG.debug(_("Injecting file path: '%s'") % path)
                self.inject_file(instance, path, contents)

        # Set admin password, if necessary
        admin_password = instance.admin_pass
        if admin_password:
            LOG.debug(_("Setting admin password"))
            self.set_admin_password(instance, admin_password)

        # Reset network config
        LOG.debug(_("Resetting network"))
        self.reset_network(instance, vm_ref)

        # Set VCPU weight
        inst_type = db.instance_type_get(ctx, instance.instance_type_id)
        vcpu_weight = inst_type["vcpu_weight"]
        if str(vcpu_weight) != "None":
            LOG.debug(_("Setting VCPU weight"))
            self._session.call_xenapi("VM.add_to_VCPUs_params", vm_ref,
                    "weight", vcpu_weight)

    def _spawn(self, instance, vm_ref):
        """Spawn a new instance."""
        LOG.debug(_('Starting VM %s...'), vm_ref)
        self._start(instance, vm_ref)
        instance_name = instance.name
        LOG.info(_('Spawning VM %(instance_name)s created %(vm_ref)s.')
                 % locals())

        ctx = nova_context.get_admin_context()
        agent_build = db.agent_build_get_by_triple(ctx, 'xen',
                              instance.os_type, instance.architecture)
        if agent_build:
            LOG.info(_('Latest agent build for %(hypervisor)s/%(os)s' + \
                       '/%(architecture)s is %(version)s') % agent_build)
        else:
            LOG.info(_('No agent build found for %(hypervisor)s/%(os)s' + \
                       '/%(architecture)s') % {
                        'hypervisor': 'xen',
                        'os': instance.os_type,
                        'architecture': instance.architecture})

        # Wait for boot to finish
        LOG.debug(_('Instance %s: waiting for running'), instance_name)
        expiration = time.time() + FLAGS.xenapi_running_timeout
        while time.time() < expiration:
            state = self.get_info(instance_name)['state']
            if state == power_state.RUNNING:
                break

            greenthread.sleep(0.5)

        LOG.debug(_('Instance %s: running'), instance_name)

        # Update agent, if necessary
        # This also waits until the agent starts
        LOG.debug(_("Querying agent version"))
        version = self.get_agent_version(instance)
        if version:
            LOG.info(_('Instance agent version: %s') % version)

        if version and agent_build and \
           cmp_version(version, agent_build['version']) < 0:
            LOG.info(_('Updating Agent to %s') % agent_build['version'])
            self.agent_update(instance, agent_build['url'],
                          agent_build['md5hash'])

        self._configure_instance(ctx, instance, vm_ref)

    def _handle_spawn_error(self, vdis, spawn_error):
        # Extract resource list from spawn_error.
        resources = []
        if spawn_error.args:
            last_arg = spawn_error.args[-1]
            if isinstance(last_arg, list):
                resources = last_arg
        if vdis:
            for vdi in vdis:
                resources.append(dict(vdi_type=vdi['vdi_type'],
                                      vdi_uuid=vdi['vdi_uuid'],
                                      file=None))

        LOG.debug(_("Resources to remove:%s"), resources)
        kernel_file = None
        ramdisk_file = None

        for item in resources:
            vdi_type = item['vdi_type']
            vdi_to_remove = item['vdi_uuid']
            if vdi_to_remove:
                try:
                    vdi_ref = self._session.call_xenapi('VDI.get_by_uuid',
                            vdi_to_remove)
                    LOG.debug(_('Removing VDI %(vdi_ref)s' +
                                '(uuid:%(vdi_to_remove)s)'), locals())
                    VMHelper.destroy_vdi(self._session, vdi_ref)
                except self.XenAPI.Failure:
                    # Vdi has already been deleted
                    LOG.debug(_("Skipping VDI destroy for %s"), vdi_to_remove)
            if item['file']:
                # There is also a file to remove.
                if vdi_type == vm_utils.ImageType.KERNEL_STR:
                    kernel_file = item['file']
                elif vdi_type == vm_utils.ImageType.RAMDISK_STR:
                    ramdisk_file = item['file']

        if kernel_file or ramdisk_file:
            LOG.debug(_("Removing kernel/ramdisk files from dom0"))
            self._destroy_kernel_ramdisk_plugin_call(kernel_file,
                                                     ramdisk_file)

    def _get_vm_opaque_ref(self, instance_or_vm):
        """
        Refactored out the common code of many methods that receive either
        a vm name or a vm instance, and want a vm instance in return.
        """
        # if instance_or_vm is a string it must be opaque ref or instance name
        if isinstance(instance_or_vm, basestring):
            obj = None
            try:
                # check for opaque ref
                obj = self._session.call_xenapi("VM.get_uuid", instance_or_vm)
                return instance_or_vm
            except self.XenAPI.Failure:
                # wasn't an opaque ref, can be an instance name
                instance_name = instance_or_vm

        # if instance_or_vm is an int/long it must be instance id
        elif isinstance(instance_or_vm, (int, long)):
            ctx = nova_context.get_admin_context()
            instance_obj = db.instance_get(ctx, instance_or_vm)
            instance_name = instance_obj.name
        else:
            instance_name = instance_or_vm.name
        vm_ref = VMHelper.lookup(self._session, instance_name)
        if vm_ref is None:
            raise exception.NotFound(_("No opaque_ref could be determined "
                    "for '%s'.") % instance_or_vm)
        return vm_ref

    def _acquire_bootlock(self, vm):
        """Prevent an instance from booting."""
        self._session.call_xenapi(
            "VM.set_blocked_operations",
            vm,
            {"start": ""})

    def _release_bootlock(self, vm):
        """Allow an instance to boot."""
        self._session.call_xenapi(
            "VM.remove_from_blocked_operations",
            vm,
            "start")

    def snapshot(self, context, instance, image_id):
        """Create snapshot from a running VM instance.

        :param context: request context
        :param instance: instance to be snapshotted
        :param image_id: id of image to upload to

        Steps involved in a XenServer snapshot:

        1. XAPI-Snapshot: Snapshotting the instance using XenAPI. This
            creates: Snapshot (Template) VM, Snapshot VBD, Snapshot VDI,
            Snapshot VHD

        2. Wait-for-coalesce: The Snapshot VDI and Instance VDI both point to
            a 'base-copy' VDI.  The base_copy is immutable and may be chained
            with other base_copies.  If chained, the base_copies
            coalesce together, so, we must wait for this coalescing to occur to
            get a stable representation of the data on disk.

        3. Push-to-glance: Once coalesced, we call a plugin on the XenServer
            that will bundle the VHDs together and then push the bundle into
            Glance.

        """
        template_vm_ref = None
        try:
            template_vm_ref, template_vdi_uuids =\
                    self._create_snapshot(instance)
            # call plugin to ship snapshot off to glance
            VMHelper.upload_image(context,
                    self._session, instance, template_vdi_uuids, image_id)
        finally:
            if template_vm_ref:
                self._destroy(instance, template_vm_ref,
                        shutdown=False, destroy_kernel_ramdisk=False)

        logging.debug(_("Finished snapshot and upload for VM %s"), instance)

    def _create_snapshot(self, instance):
        #TODO(sirp): Add quiesce and VSS locking support when Windows support
        # is added

        logging.debug(_("Starting snapshot for VM %s"), instance)
        vm_ref = VMHelper.lookup(self._session, instance.name)

        label = "%s-snapshot" % instance.name
        try:
            template_vm_ref, template_vdi_uuids = VMHelper.create_snapshot(
                    self._session, instance, vm_ref, label)
            return template_vm_ref, template_vdi_uuids
        except self.XenAPI.Failure, exc:
            logging.error(_("Unable to Snapshot %(vm_ref)s: %(exc)s")
                    % locals())
            return

    def _migrate_vhd(self, instance, vdi_uuid, dest, sr_path):
        instance_id = instance['id']
        instance_uuid = instance['uuid']
        params = {'host': dest,
                  'vdi_uuid': vdi_uuid,
                  'instance_uuid': instance_uuid,
                  'sr_path': sr_path}

        try:
            _params = {'params': pickle.dumps(params)}
            task = self._session.async_call_plugin('migration',
                                                   'transfer_vhd',
                                                   _params)
            self._session.wait_for_task(task, instance_uuid)
        except self.XenAPI.Failure:
            msg = _("Failed to transfer vhd to new host")
            raise exception.MigrationError(reason=msg)

    def _get_orig_vm_name_label(self, instance):
        return instance.name + '-orig'

    def _update_instance_progress(self, context, instance, step, total_steps):
        """Update instance progress percent to reflect current step number
        """
        # FIXME(sirp): for now we're taking a KISS approach to instance
        # progress:
        # Divide the action's workflow into discrete steps and "bump" the
        # instance's progress field as each step is completed.
        #
        # For a first cut this should be fine, however, for large VM images,
        # the _create_disks step begins to dominate the equation. A
        # better approximation would use the percentage of the VM image that
        # has been streamed to the destination host.
        progress = round(float(step) / total_steps * 100)
        instance_uuid = instance['uuid']
        LOG.debug(_("Updating instance '%(instance_uuid)s' progress to"
                    " %(progress)d") % locals())
        db.instance_update(context, instance_uuid, {'progress': progress})

    def migrate_disk_and_power_off(self, context, instance, dest,
                                   instance_type):
        """Copies a VHD from one host machine to another, possibly
        resizing filesystem before hand.

        :param instance: the instance that owns the VHD in question.
        :param dest: the destination host machine.
        :param disk_type: values are 'primary' or 'cow'.

        """
        # 0. Zero out the progress to begin
        self._update_instance_progress(context, instance,
                                       step=0,
                                       total_steps=RESIZE_TOTAL_STEPS)

        vm_ref = VMHelper.lookup(self._session, instance.name)

        # The primary VDI becomes the COW after the snapshot, and we can
        # identify it via the VBD. The base copy is the parent_uuid returned
        # from the snapshot creation

        base_copy_uuid = cow_uuid = None
        template_vdi_uuids = template_vm_ref = None
        try:
            # 1. Create Snapshot
            template_vm_ref, template_vdi_uuids =\
                    self._create_snapshot(instance)
            self._update_instance_progress(context, instance,
                                           step=1,
                                           total_steps=RESIZE_TOTAL_STEPS)

            base_copy_uuid = template_vdi_uuids['image']
            vdi_ref, vm_vdi_rec = \
                    VMHelper.get_vdi_for_vm_safely(self._session, vm_ref)
            cow_uuid = vm_vdi_rec['uuid']

            sr_path = VMHelper.get_sr_path(self._session)

            if instance['auto_disk_config'] and \
               instance['local_gb'] > instance_type['local_gb']:
                # Resizing disk storage down
                old_gb = instance['local_gb']
                new_gb = instance_type['local_gb']

                LOG.debug(_("Resizing down VDI %(cow_uuid)s from "
                          "%(old_gb)dGB to %(new_gb)dGB") % locals())

                # 2. Power down the instance before resizing
                self._shutdown(instance, vm_ref, hard=False)
                self._update_instance_progress(context, instance,
                                               step=2,
                                               total_steps=RESIZE_TOTAL_STEPS)

                # 3. Copy VDI, resize partition and filesystem, forget VDI,
                # truncate VHD
                new_ref, new_uuid = VMHelper.resize_disk(self._session,
                                                         vdi_ref,
                                                         instance_type)
                self._update_instance_progress(context, instance,
                                               step=3,
                                               total_steps=RESIZE_TOTAL_STEPS)

                # 4. Transfer the new VHD
                self._migrate_vhd(instance, new_uuid, dest, sr_path)
                self._update_instance_progress(context, instance,
                                               step=4,
                                               total_steps=RESIZE_TOTAL_STEPS)

                # Clean up VDI now that it's been copied
                VMHelper.destroy_vdi(self._session, new_ref)

                vdis = {'base_copy': new_uuid}
            else:
                # Resizing disk storage up, will be handled on destination

                # As an optimization, we transfer the base VDI first,
                # then shut down the VM, followed by transfering the COW
                # VDI.

                # 2. Transfer the base copy
                self._migrate_vhd(instance, base_copy_uuid, dest, sr_path)
                self._update_instance_progress(context, instance,
                                               step=2,
                                               total_steps=RESIZE_TOTAL_STEPS)

                # 3. Now power down the instance
                self._shutdown(instance, vm_ref, hard=False)
                self._update_instance_progress(context, instance,
                                               step=3,
                                               total_steps=RESIZE_TOTAL_STEPS)

                # 4. Transfer the COW VHD
                self._migrate_vhd(instance, cow_uuid, dest, sr_path)
                self._update_instance_progress(context, instance,
                                               step=4,
                                               total_steps=RESIZE_TOTAL_STEPS)

                # TODO(mdietz): we could also consider renaming these to
                # something sensible so we don't need to blindly pass
                # around dictionaries
                vdis = {'base_copy': base_copy_uuid, 'cow': cow_uuid}

            # NOTE(sirp): in case we're resizing to the same host (for dev
            # purposes), apply a suffix to name-label so the two VM records
            # extant until a confirm_resize don't collide.
            name_label = self._get_orig_vm_name_label(instance)
            VMHelper.set_vm_name_label(self._session, vm_ref, name_label)
        finally:
            if template_vm_ref:
                self._destroy(instance, template_vm_ref,
                        shutdown=False, destroy_kernel_ramdisk=False)

        return vdis

    def _move_disks(self, instance, disk_info):
        """Move and possibly link VHDs via the XAPI plugin."""
        base_copy_uuid = disk_info['base_copy']
        new_base_copy_uuid = str(uuid.uuid4())

        params = {'instance_uuid': instance['uuid'],
                  'sr_path': VMHelper.get_sr_path(self._session),
                  'old_base_copy_uuid': base_copy_uuid,
                  'new_base_copy_uuid': new_base_copy_uuid}

        if 'cow' in disk_info:
            cow_uuid = disk_info['cow']
            new_cow_uuid = str(uuid.uuid4())
            params['old_cow_uuid'] = cow_uuid
            params['new_cow_uuid'] = new_cow_uuid

            new_uuid = new_cow_uuid
        else:
            new_uuid = new_base_copy_uuid

        task = self._session.async_call_plugin('migration',
                'move_vhds_into_sr', {'params': pickle.dumps(params)})
        self._session.wait_for_task(task, instance['uuid'])

        # Now we rescan the SR so we find the VHDs
        VMHelper.scan_default_sr(self._session)

        # Set name-label so we can find if we need to clean up a failed
        # migration
        VMHelper.set_vdi_name_label(self._session, new_uuid,
                                    instance.name)

        return new_uuid

    def _resize_instance(self, instance, vdi_uuid):
        """Resize a running instance by changing its disk size."""
        #TODO(mdietz): this will need to be adjusted for swap later

        new_disk_size = instance.local_gb * 1024 * 1024 * 1024
        if not new_disk_size:
            return

        # Get current size of VDI
        vdi_ref = self._session.call_xenapi('VDI.get_by_uuid', vdi_uuid)
        virtual_size = self._session.call_xenapi('VDI.get_virtual_size',
                                                 vdi_ref)
        virtual_size = int(virtual_size)

        instance_name = instance.name
        old_gb = virtual_size / (1024 * 1024 * 1024)
        new_gb = instance.local_gb

        if virtual_size < new_disk_size:
            # Resize up. Simple VDI resize will do the trick
            LOG.debug(_("Resizing up VDI %(vdi_uuid)s from %(old_gb)dGB to "
                        "%(new_gb)dGB") % locals())
            if self._product_version[0] > 5:
                resize_func_name = 'VDI.resize'
            else:
                resize_func_name = 'VDI.resize_online'
            self._session.call_xenapi(resize_func_name, vdi_ref,
                    str(new_disk_size))
            LOG.debug(_("Resize instance %s complete") % (instance.name))

    def reboot(self, instance, reboot_type):
        """Reboot VM instance."""
        vm_ref = self._get_vm_opaque_ref(instance)

        if reboot_type == "HARD":
            task = self._session.call_xenapi('Async.VM.hard_reboot', vm_ref)
        else:
            task = self._session.call_xenapi('Async.VM.clean_reboot', vm_ref)

        self._session.wait_for_task(task, instance['uuid'])

    def get_agent_version(self, instance):
        """Get the version of the agent running on the VM instance."""

        # The agent can be slow to start for a variety of reasons. On Windows,
        # it will generally perform a setup process on first boot that can
        # take a couple of minutes and then reboot. On Linux, the system can
        # also take a while to boot. So we need to be more patient than
        # normal as well as watch for domid changes

        def _call():
            # Send the encrypted password
            transaction_id = str(uuid.uuid4())
            args = {'id': transaction_id}
            resp = self._make_agent_call('version', instance, '', args)
            if resp['returncode'] != '0':
                LOG.error(_('Failed to query agent version: %(resp)r') %
                          locals())
                return None
            # Some old versions of the Windows agent have a trailing \\r\\n
            # (ie CRLF escaped) for some reason. Strip that off.
            return resp['message'].replace('\\r\\n', '')

        vm_ref = self._get_vm_opaque_ref(instance)
        vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)

        domid = vm_rec['domid']

        expiration = time.time() + FLAGS.agent_version_timeout
        while time.time() < expiration:
            ret = _call()
            if ret:
                return ret

            vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)
            if vm_rec['domid'] != domid:
                LOG.info(_('domid changed from %(olddomid)s to '
                           '%(newdomid)s') % {
                               'olddomid': domid,
                                'newdomid': vm_rec['domid']})
                domid = vm_rec['domid']

        return None

    def agent_update(self, instance, url, md5sum):
        """Update agent on the VM instance."""

        # Send the encrypted password
        transaction_id = str(uuid.uuid4())
        args = {'id': transaction_id, 'url': url, 'md5sum': md5sum}
        resp = self._make_agent_call('agentupdate', instance, '', args)
        if resp['returncode'] != '0':
            LOG.error(_('Failed to update agent: %(resp)r') % locals())
            return None
        return resp['message']

    def set_admin_password(self, instance, new_pass):
        """Set the root/admin password on the VM instance.

        This is done via an agent running on the VM. Communication between nova
        and the agent is done via writing xenstore records. Since communication
        is done over the XenAPI RPC calls, we need to encrypt the password.
        We're using a simple Diffie-Hellman class instead of the more advanced
        one in M2Crypto for compatibility with the agent code.

        """
        # Need to uniquely identify this request.
        key_init_transaction_id = str(uuid.uuid4())
        # The simple Diffie-Hellman class is used to manage key exchange.
        dh = SimpleDH()
        key_init_args = {'id': key_init_transaction_id,
                         'pub': str(dh.get_public())}
        resp = self._make_agent_call('key_init', instance, '', key_init_args)
        # Successful return code from key_init is 'D0'
        if resp['returncode'] != 'D0':
            LOG.error(_('Failed to exchange keys: %(resp)r') % locals())
            return None
        # Some old versions of the Windows agent have a trailing \\r\\n
        # (ie CRLF escaped) for some reason. Strip that off.
        agent_pub = int(resp['message'].replace('\\r\\n', ''))
        dh.compute_shared(agent_pub)
        # Some old versions of Linux and Windows agent expect trailing \n
        # on password to work correctly.
        enc_pass = dh.encrypt(new_pass + '\n')
        # Send the encrypted password
        password_transaction_id = str(uuid.uuid4())
        password_args = {'id': password_transaction_id, 'enc_pass': enc_pass}
        resp = self._make_agent_call('password', instance, '', password_args)
        # Successful return code from password is '0'
        if resp['returncode'] != '0':
            LOG.error(_('Failed to update password: %(resp)r') % locals())
            return None
        return resp['message']

    def inject_file(self, instance, path, contents):
        """Write a file to the VM instance.

        The path to which it is to be written and the contents of the file
        need to be supplied; both will be base64-encoded to prevent errors
        with non-ASCII characters being transmitted. If the agent does not
        support file injection, or the user has disabled it, a
        NotImplementedError will be raised.

        """
        # Files/paths must be base64-encoded for transmission to agent
        b64_path = base64.b64encode(path)
        b64_contents = base64.b64encode(contents)

        # Need to uniquely identify this request.
        transaction_id = str(uuid.uuid4())
        args = {'id': transaction_id, 'b64_path': b64_path,
                'b64_contents': b64_contents}
        # If the agent doesn't support file injection, a NotImplementedError
        # will be raised with the appropriate message.
        resp = self._make_agent_call('inject_file', instance, '', args)
        if resp['returncode'] != '0':
            LOG.error(_('Failed to inject file: %(resp)r') % locals())
            return None
        return resp['message']

    def _shutdown(self, instance, vm_ref, hard=True):
        """Shutdown an instance."""
        state = self.get_info(instance['name'])['state']
        if state == power_state.SHUTDOWN:
            instance_name = instance.name
            LOG.warn(_("VM %(instance_name)s already halted,"
                    "skipping shutdown...") % locals())
            return

        instance_uuid = instance['uuid']
        LOG.debug(_("Shutting down VM for Instance %(instance_uuid)s")
                  % locals())
        try:
            task = None
            if hard:
                task = self._session.call_xenapi("Async.VM.hard_shutdown",
                                                 vm_ref)
            else:
                task = self._session.call_xenapi("Async.VM.clean_shutdown",
                                                 vm_ref)
            self._session.wait_for_task(task, instance['uuid'])
        except self.XenAPI.Failure, exc:
            LOG.exception(exc)

    def _find_rescue_vbd_ref(self, vm_ref, rescue_vm_ref):
        """Find and return the rescue VM's vbd_ref.

        We use the second VBD here because swap is first with the root file
        system coming in second."""
        vbd_ref = self._session.call_xenapi("VM.get_VBDs", vm_ref)[1]
        vdi_ref = self._session.call_xenapi("VBD.get_record", vbd_ref)["VDI"]

        return VolumeHelper.create_vbd(self._session, rescue_vm_ref, vdi_ref,
                1, False)

    def _shutdown_rescue(self, rescue_vm_ref):
        """Shutdown a rescue instance."""
        self._session.call_xenapi("Async.VM.hard_shutdown", rescue_vm_ref)

    def _destroy_vdis(self, instance, vm_ref):
        """Destroys all VDIs associated with a VM."""
        instance_uuid = instance['uuid']
        LOG.debug(_("Destroying VDIs for Instance %(instance_uuid)s")
                  % locals())
        vdi_refs = VMHelper.lookup_vm_vdis(self._session, vm_ref)

        if not vdi_refs:
            return

        for vdi_ref in vdi_refs:
            try:
                task = self._session.call_xenapi('Async.VDI.destroy', vdi_ref)
                self._session.wait_for_task(task, instance['uuid'])
            except self.XenAPI.Failure, exc:
                LOG.exception(exc)

    def _destroy_rescue_vdis(self, rescue_vm_ref):
        """Destroys all VDIs associated with a rescued VM."""
        vdi_refs = VMHelper.lookup_vm_vdis(self._session, rescue_vm_ref)
        for vdi_ref in vdi_refs:
            try:
                self._session.call_xenapi("Async.VDI.destroy", vdi_ref)
            except self.XenAPI.Failure:
                continue

    def _destroy_rescue_vbds(self, rescue_vm_ref):
        """Destroys all VBDs tied to a rescue VM."""
        vbd_refs = self._session.call_xenapi("VM.get_VBDs", rescue_vm_ref)
        for vbd_ref in vbd_refs:
            vbd_rec = self._session.call_xenapi("VBD.get_record", vbd_ref)
            if vbd_rec.get("userdevice", None) == "1":  # VBD is always 1
                VMHelper.unplug_vbd(self._session, vbd_ref)
                VMHelper.destroy_vbd(self._session, vbd_ref)

    def _destroy_kernel_ramdisk_plugin_call(self, kernel, ramdisk):
        args = {}
        if kernel:
            args['kernel-file'] = kernel
        if ramdisk:
            args['ramdisk-file'] = ramdisk
        task = self._session.async_call_plugin(
            'glance', 'remove_kernel_ramdisk', args)
        self._session.wait_for_task(task)

    def _destroy_kernel_ramdisk(self, instance, vm_ref):
        """Three situations can occur:

            1. We have neither a ramdisk nor a kernel, in which case we are a
               RAW image and can omit this step

            2. We have one or the other, in which case, we should flag as an
               error

            3. We have both, in which case we safely remove both the kernel
               and the ramdisk.

        """
        instance_uuid = instance['uuid']
        if not instance.kernel_id and not instance.ramdisk_id:
            # 1. No kernel or ramdisk
            LOG.debug(_("Instance %(instance_uuid)s using RAW or VHD, "
                        "skipping kernel and ramdisk deletion") % locals())
            return

        if not (instance.kernel_id and instance.ramdisk_id):
            # 2. We only have kernel xor ramdisk
            raise exception.InstanceUnacceptable(instance_id=instance_uuid,
               reason=_("instance has a kernel or ramdisk but not both"))

        # 3. We have both kernel and ramdisk
        (kernel, ramdisk) = VMHelper.lookup_kernel_ramdisk(self._session,
                                                           vm_ref)

        self._destroy_kernel_ramdisk_plugin_call(kernel, ramdisk)
        LOG.debug(_("kernel/ramdisk files removed"))

    def _destroy_vm(self, instance, vm_ref):
        """Destroys a VM record."""
        instance_id = instance['id']
        instance_uuid = instance['uuid']
        try:
            task = self._session.call_xenapi('Async.VM.destroy', vm_ref)
            self._session.wait_for_task(task, instance_uuid)
        except self.XenAPI.Failure, exc:
            LOG.exception(exc)

        LOG.debug(_("Instance %(instance_uuid)s VM destroyed") % locals())

    def _destroy_rescue_instance(self, rescue_vm_ref):
        """Destroy a rescue instance."""
        self._destroy_rescue_vbds(rescue_vm_ref)
        self._shutdown_rescue(rescue_vm_ref)
        self._destroy_rescue_vdis(rescue_vm_ref)

        self._session.call_xenapi("Async.VM.destroy", rescue_vm_ref)

    def destroy(self, instance, network_info):
        """Destroy VM instance.

        This is the method exposed by xenapi_conn.destroy(). The rest of the
        destroy_* methods are internal.

        """
        instance_uuid = instance['uuid']
        LOG.info(_("Destroying VM for Instance %(instance_uuid)s") % locals())
        vm_ref = VMHelper.lookup(self._session, instance.name)
        return self._destroy(instance, vm_ref, network_info, shutdown=True)

    def _destroy(self, instance, vm_ref, network_info=None, shutdown=True,
                 destroy_kernel_ramdisk=True):
        """Destroys VM instance by performing:

            1. A shutdown if requested.
            2. Destroying associated VDIs.
            3. Destroying kernel and ramdisk files (if necessary).
            4. Destroying that actual VM record.

        """
        if vm_ref is None:
            LOG.warning(_("VM is not present, skipping destroy..."))
            return

        if shutdown:
            self._shutdown(instance, vm_ref)

        self._destroy_vdis(instance, vm_ref)
        if destroy_kernel_ramdisk:
            self._destroy_kernel_ramdisk(instance, vm_ref)
        self._destroy_vm(instance, vm_ref)

        self.unplug_vifs(instance, network_info)

    def pause(self, instance):
        """Pause VM instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        task = self._session.call_xenapi('Async.VM.pause', vm_ref)
        self._session.wait_for_task(task, instance['uuid'])

    def unpause(self, instance):
        """Unpause VM instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        task = self._session.call_xenapi('Async.VM.unpause', vm_ref)
        self._session.wait_for_task(task, instance['uuid'])

    def suspend(self, instance):
        """Suspend the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        task = self._session.call_xenapi('Async.VM.suspend', vm_ref)
        self._session.wait_for_task(task, instance['uuid'])

    def resume(self, instance):
        """Resume the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        task = self._session.call_xenapi('Async.VM.resume',
                                         vm_ref, False, True)
        self._session.wait_for_task(task, instance['uuid'])

    def rescue(self, context, instance, network_info, image_meta):
        """Rescue the specified instance.

            - shutdown the instance VM.
            - set 'bootlock' to prevent the instance from starting in rescue.
            - spawn a rescue VM (the vm name-label will be instance-N-rescue).

        """
        rescue_vm_ref = VMHelper.lookup(self._session,
                                        "%s-rescue" % instance.name)
        if rescue_vm_ref:
            raise RuntimeError(_(
                "Instance is already in Rescue Mode: %s" % instance.name))

        vm_ref = VMHelper.lookup(self._session, instance.name)
        self._shutdown(instance, vm_ref)
        self._acquire_bootlock(vm_ref)
        instance._rescue = True
        self.spawn_rescue(context, instance, image_meta, network_info)
        rescue_vm_ref = VMHelper.lookup(self._session, instance.name)
        rescue_vbd_ref = self._find_rescue_vbd_ref(vm_ref, rescue_vm_ref)

        self._session.call_xenapi("Async.VBD.plug", rescue_vbd_ref)

    def unrescue(self, instance):
        """Unrescue the specified instance.

            - unplug the instance VM's disk from the rescue VM.
            - teardown the rescue VM.
            - release the bootlock to allow the instance VM to start.

        """
        rescue_vm_ref = VMHelper.lookup(self._session,
                                        "%s-rescue" % instance.name)

        if not rescue_vm_ref:
            raise exception.InstanceNotInRescueMode(instance_id=instance.uuid)

        original_vm_ref = VMHelper.lookup(self._session, instance.name)
        instance._rescue = False

        self._destroy_rescue_instance(rescue_vm_ref)
        self._release_bootlock(original_vm_ref)
        self._start(instance, original_vm_ref)

    def power_off(self, instance):
        """Power off the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        self._shutdown(instance, vm_ref, hard=True)

    def power_on(self, instance):
        """Power on the specified instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        self._start(instance, vm_ref)

    def _cancel_stale_tasks(self, timeout, task):
        """Cancel the given tasks that are older than the given timeout."""
        task_refs = self._session.call_xenapi("task.get_by_name_label", task)
        for task_ref in task_refs:
            task_rec = self._session.call_xenapi("task.get_record", task_ref)
            task_created = utils.parse_strtime(task_rec["created"].value,
                    "%Y%m%dT%H:%M:%SZ")

            if utils.is_older_than(task_created, timeout):
                self._session.call_xenapi("task.cancel", task_ref)

    def poll_rebooting_instances(self, timeout):
        """Look for expirable rebooting instances.

            - issue a "hard" reboot to any instance that has been stuck in a
              reboot state for >= the given timeout
        """
        # NOTE(jk0): All existing clean_reboot tasks must be cancelled before
        # we can kick off the hard_reboot tasks.
        self._cancel_stale_tasks(timeout, "Async.VM.clean_reboot")

        ctxt = nova_context.get_admin_context()
        instances = db.instance_get_all_hung_in_rebooting(ctxt, timeout)

        instances_info = dict(instance_count=len(instances),
                timeout=timeout)

        if instances_info["instance_count"] > 0:
            LOG.info(_("Found %(instance_count)d hung reboots "
                    "older than %(timeout)d seconds") % instances_info)

        for instance in instances:
            LOG.info(_("Automatically hard rebooting %d"), instance.id)
            self.compute_api.reboot(ctxt, instance, "HARD")

    def poll_rescued_instances(self, timeout):
        """Look for expirable rescued instances.

            - forcibly exit rescue mode for any instances that have been
              in rescue mode for >= the provided timeout

        """
        last_ran = self.poll_rescue_last_ran
        if not last_ran:
            # We need a base time to start tracking.
            self.poll_rescue_last_ran = utils.utcnow()
            return

        if not utils.is_older_than(last_ran, timeout):
            # Do not run. Let's bail.
            return

        # Update the time tracker and proceed.
        self.poll_rescue_last_ran = utils.utcnow()

        rescue_vms = []
        for instance in self.list_instances():
            if instance.endswith("-rescue"):
                rescue_vms.append(dict(name=instance,
                                       vm_ref=VMHelper.lookup(self._session,
                                                              instance)))

        for vm in rescue_vms:
            rescue_vm_ref = vm["vm_ref"]

            self._destroy_rescue_instance(rescue_vm_ref)

            original_name = vm["name"].split("-rescue", 1)[0]
            original_vm_ref = VMHelper.lookup(self._session, original_name)

            self._release_bootlock(original_vm_ref)
            self._session.call_xenapi("VM.start", original_vm_ref, False,
                                      False)

    def poll_unconfirmed_resizes(self, resize_confirm_window):
        """Poll for unconfirmed resizes.

        Look for any unconfirmed resizes that are older than
        `resize_confirm_window` and automatically confirm them.
        """
        ctxt = nova_context.get_admin_context()
        migrations = db.migration_get_all_unconfirmed(ctxt,
            resize_confirm_window)

        migrations_info = dict(migration_count=len(migrations),
                confirm_window=FLAGS.resize_confirm_window)

        if migrations_info["migration_count"] > 0:
            LOG.info(_("Found %(migration_count)d unconfirmed migrations "
                    "older than %(confirm_window)d seconds") % migrations_info)

        for migration in migrations:
            LOG.info(_("Automatically confirming migration %d"), migration.id)
            self.compute_api.confirm_resize(ctxt, migration.instance_uuid)

    def get_info(self, instance):
        """Return data about VM instance."""
        vm_ref = self._get_vm_opaque_ref(instance)
        vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)
        return VMHelper.compile_info(vm_rec)

    def get_diagnostics(self, instance):
        """Return data about VM diagnostics."""
        vm_ref = self._get_vm_opaque_ref(instance)
        vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)
        return VMHelper.compile_diagnostics(self._session, vm_rec)

    def get_all_bw_usage(self, start_time, stop_time=None):
        """Return bandwidth usage info for each interface on each
           running VM"""
        try:
            metrics = VMHelper.compile_metrics(self._session,
                                               start_time,
                                               stop_time)
        except exception.CouldNotFetchMetrics:
            LOG.exception(_("Could not get bandwidth info."),
                          exc_info=sys.exc_info())
        bw = {}
        for uuid, data in metrics.iteritems():
            vm_ref = self._session.call_xenapi("VM.get_by_uuid", uuid)
            vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)
            vif_map = {}
            for vif in [self._session.call_xenapi("VIF.get_record", vrec)
                        for vrec in vm_rec['VIFs']]:
                vif_map[vif['device']] = vif['MAC']
            name = vm_rec['name_label']
            if name.startswith('Control domain'):
                continue
            vifs_bw = bw.setdefault(name, {})
            for key, val in data.iteritems():
                if key.startswith('vif_'):
                    vname = key.split('_')[1]
                    vif_bw = vifs_bw.setdefault(vif_map[vname], {})
                    if key.endswith('tx'):
                        vif_bw['bw_out'] = int(val)
                    if key.endswith('rx'):
                        vif_bw['bw_in'] = int(val)
        return bw

    def get_console_output(self, instance):
        """Return snapshot of console."""
        # TODO: implement this to fix pylint!
        return 'FAKE CONSOLE OUTPUT of instance'

    def get_ajax_console(self, instance):
        """Return link to instance's ajax console."""
        # TODO: implement this!
        return 'http://fakeajaxconsole/fake_url'

    def host_power_action(self, host, action):
        """Reboots or shuts down the host."""
        args = {"action": json.dumps(action)}
        methods = {"reboot": "host_reboot", "shutdown": "host_shutdown"}
        json_resp = self._call_xenhost(methods[action], args)
        resp = json.loads(json_resp)
        return resp["power_action"]

    def set_host_enabled(self, host, enabled):
        """Sets the specified host's ability to accept new instances."""
        args = {"enabled": json.dumps(enabled)}
        xenapi_resp = self._call_xenhost("set_host_enabled", args)
        try:
            resp = json.loads(xenapi_resp)
        except TypeError as e:
            # Already logged; return the message
            return xenapi_resp.details[-1]
        return resp["status"]

    def _call_xenhost(self, method, arg_dict):
        """There will be several methods that will need this general
        handling for interacting with the xenhost plugin, so this abstracts
        out that behavior.
        """
        # Create a task ID as something that won't match any instance ID
        task_id = random.randint(-80000, -70000)
        try:
            task = self._session.async_call_plugin("xenhost", method,
                    args=arg_dict)
                    #args={"params": arg_dict})
            ret = self._session.wait_for_task(task, str(task_id))
        except self.XenAPI.Failure as e:
            ret = e
            LOG.error(_("The call to %(method)s returned an error: %(e)s.")
                    % locals())
        return ret

    def inject_network_info(self, instance, network_info, vm_ref=None):
        """
        Generate the network info and make calls to place it into the
        xenstore and the xenstore param list.
        vm_ref can be passed in because it will sometimes be different than
        what VMHelper.lookup(session, instance.name) will find (ex: rescue)
        """
        if vm_ref:
            # this function raises if vm_ref is not a vm_opaque_ref
            self._session.call_xenapi("VM.get_record", vm_ref)
        else:
            vm_ref = VMHelper.lookup(self._session, instance.name)
        logging.debug(_("injecting network info to xs for vm: |%s|"), vm_ref)

        for (network, info) in network_info:
            location = 'vm-data/networking/%s' % info['mac'].replace(':', '')
            self.write_to_param_xenstore(vm_ref, {location: info})
            try:
                # TODO(tr3buchet): fix function call after refactor
                #self.write_to_xenstore(vm_ref, location, info)
                self._make_plugin_call('xenstore.py', 'write_record', instance,
                                       location, {'value': json.dumps(info)},
                                       vm_ref)
            except KeyError:
                # catch KeyError for domid if instance isn't running
                pass

    def create_vifs(self, vm_ref, instance, network_info):
        """Creates vifs for an instance."""

        logging.debug(_("creating vif(s) for vm: |%s|"), vm_ref)

        # this function raises if vm_ref is not a vm_opaque_ref
        self._session.call_xenapi("VM.get_record", vm_ref)

        for device, (network, info) in enumerate(network_info):
            vif_rec = self.vif_driver.plug(instance, network, info,
                                           vm_ref=vm_ref, device=device)
            network_ref = vif_rec['network']
            LOG.debug(_('Creating VIF for VM %(vm_ref)s,' \
                ' network %(network_ref)s.') % locals())
            vif_ref = self._session.call_xenapi('VIF.create', vif_rec)
            LOG.debug(_('Created VIF %(vif_ref)s for VM %(vm_ref)s,'
                ' network %(network_ref)s.') % locals())

    def plug_vifs(self, instance, network_info):
        """Set up VIF networking on the host."""
        for device, (network, mapping) in enumerate(network_info):
            self.vif_driver.plug(instance, network, mapping, device=device)

    def unplug_vifs(self, instance, network_info):
        if network_info:
            for (network, mapping) in network_info:
                self.vif_driver.unplug(instance, network, mapping)

    def reset_network(self, instance, vm_ref=None):
        """Creates uuid arg to pass to make_agent_call and calls it."""
        if not vm_ref:
            vm_ref = VMHelper.lookup(self._session, instance.name)
        args = {'id': str(uuid.uuid4())}
        # TODO(tr3buchet): fix function call after refactor
        #resp = self._make_agent_call('resetnetwork', instance, '', args)
        resp = self._make_plugin_call('agent', 'resetnetwork', instance, '',
                                                               args, vm_ref)

    def inject_hostname(self, instance, vm_ref, hostname):
        """Inject the hostname of the instance into the xenstore."""
        if instance.os_type == "windows":
            # NOTE(jk0): Windows hostnames can only be <= 15 chars.
            hostname = hostname[:15]

        logging.debug(_("injecting hostname to xs for vm: |%s|"), vm_ref)
        self._session.call_xenapi_request("VM.add_to_xenstore_data",
                (vm_ref, "vm-data/hostname", hostname))

    def list_from_xenstore(self, vm, path):
        """
        Runs the xenstore-ls command to get a listing of all records
        from 'path' downward. Returns a dict with the sub-paths as keys,
        and the value stored in those paths as values. If nothing is
        found at that path, returns None.
        """
        ret = self._make_xenstore_call('list_records', vm, path)
        return json.loads(ret)

    def read_from_xenstore(self, vm, path):
        """
        Returns the value stored in the xenstore record for the given VM
        at the specified location. A XenAPIPlugin.PluginError will be raised
        if any error is encountered in the read process.
        """
        try:
            ret = self._make_xenstore_call('read_record', vm, path,
                    {'ignore_missing_path': 'True'})
        except self.XenAPI.Failure:
            return None
        ret = json.loads(ret)
        if ret == "None":
            # Can't marshall None over RPC calls.
            return None
        return ret

    def write_to_xenstore(self, vm, path, value):
        """
        Writes the passed value to the xenstore record for the given VM
        at the specified location. A XenAPIPlugin.PluginError will be raised
        if any error is encountered in the write process.
        """
        return self._make_xenstore_call('write_record', vm, path,
                {'value': json.dumps(value)})

    def clear_xenstore(self, vm, path):
        """
        Deletes the VM's xenstore record for the specified path.
        If there is no such record, the request is ignored.
        """
        self._make_xenstore_call('delete_record', vm, path)

    def _make_xenstore_call(self, method, vm, path, addl_args=None):
        """Handles calls to the xenstore xenapi plugin."""
        return self._make_plugin_call('xenstore.py', method=method, vm=vm,
                path=path, addl_args=addl_args)

    def _make_agent_call(self, method, vm, path, addl_args=None):
        """Abstracts out the interaction with the agent xenapi plugin."""
        ret = self._make_plugin_call('agent', method=method, vm=vm,
                path=path, addl_args=addl_args)
        if isinstance(ret, dict):
            return ret
        try:
            return json.loads(ret)
        except TypeError:
            instance_uuid = vm['uuid']
            LOG.error(_('The agent call to %(method)s returned an invalid'
                      ' response: %(ret)r. VM id=%(instance_uuid)s;'
                      ' path=%(path)s; args=%(addl_args)r') % locals())
            return {'returncode': 'error',
                    'message': 'unable to deserialize response'}

    def _make_plugin_call(self, plugin, method, vm, path, addl_args=None,
                                                          vm_ref=None):
        """
        Abstracts out the process of calling a method of a xenapi plugin.
        Any errors raised by the plugin will in turn raise a RuntimeError here.
        """
        instance_id = vm['id']
        instance_uuid = vm['uuid']
        vm_ref = vm_ref or self._get_vm_opaque_ref(vm)
        vm_rec = self._session.call_xenapi("VM.get_record", vm_ref)
        args = {'dom_id': vm_rec['domid'], 'path': path}
        args.update(addl_args or {})
        try:
            task = self._session.async_call_plugin(plugin, method, args)
            ret = self._session.wait_for_task(task, instance_uuid)
        except self.XenAPI.Failure, e:
            ret = None
            err_msg = e.details[-1].splitlines()[-1]
            if 'TIMEOUT:' in err_msg:
                LOG.error(_('TIMEOUT: The call to %(method)s timed out. '
                        'VM id=%(instance_uuid)s; args=%(args)r') % locals())
                return {'returncode': 'timeout', 'message': err_msg}
            elif 'NOT IMPLEMENTED:' in err_msg:
                LOG.error(_('NOT IMPLEMENTED: The call to %(method)s is not'
                        ' supported by the agent. VM id=%(instance_uuid)s;'
                        ' args=%(args)r') % locals())
                return {'returncode': 'notimplemented', 'message': err_msg}
            else:
                LOG.error(_('The call to %(method)s returned an error: %(e)s. '
                        'VM id=%(instance_uuid)s; args=%(args)r') % locals())
                return {'returncode': 'error', 'message': err_msg}
        return ret

    def add_to_xenstore(self, vm, path, key, value):
        """
        Adds the passed key/value pair to the xenstore record for
        the given VM at the specified location. A XenAPIPlugin.PluginError
        will be raised if any error is encountered in the write process.
        """
        current = self.read_from_xenstore(vm, path)
        if not current:
            # Nothing at that location
            current = {key: value}
        else:
            current[key] = value
        self.write_to_xenstore(vm, path, current)

    def remove_from_xenstore(self, vm, path, key_or_keys):
        """
        Takes either a single key or a list of keys and removes
        them from the xenstoreirecord data for the given VM.
        If the key doesn't exist, the request is ignored.
        """
        current = self.list_from_xenstore(vm, path)
        if not current:
            return
        if isinstance(key_or_keys, basestring):
            keys = [key_or_keys]
        else:
            keys = key_or_keys
        keys.sort(lambda x, y: cmp(y.count('/'), x.count('/')))
        for key in keys:
            if path:
                keypath = "%s/%s" % (path, key)
            else:
                keypath = key
            self._make_xenstore_call('delete_record', vm, keypath)

    ########################################################################
    ###### The following methods interact with the xenstore parameter
    ###### record, not the live xenstore. They were created before I
    ###### knew the difference, and are left in here in case they prove
    ###### to be useful. They all have '_param' added to their method
    ###### names to distinguish them. (dabo)
    ########################################################################
    def read_partial_from_param_xenstore(self, instance_or_vm, key_prefix):
        """
        Returns a dict of all the keys in the xenstore parameter record
        for the given instance that begin with the key_prefix.
        """
        data = self.read_from_param_xenstore(instance_or_vm)
        badkeys = [k for k in data.keys()
                if not k.startswith(key_prefix)]
        for badkey in badkeys:
            del data[badkey]
        return data

    def read_from_param_xenstore(self, instance_or_vm, keys=None):
        """
        Returns the xenstore parameter record data for the specified VM
        instance as a dict. Accepts an optional key or list of keys; if a
        value for 'keys' is passed, the returned dict is filtered to only
        return the values for those keys.
        """
        vm_ref = self._get_vm_opaque_ref(instance_or_vm)
        data = self._session.call_xenapi_request('VM.get_xenstore_data',
                (vm_ref,))
        ret = {}
        if keys is None:
            keys = data.keys()
        elif isinstance(keys, basestring):
            keys = [keys]
        for key in keys:
            raw = data.get(key)
            if raw:
                ret[key] = json.loads(raw)
            else:
                ret[key] = raw
        return ret

    def add_to_param_xenstore(self, instance_or_vm, key, val):
        """
        Takes a key/value pair and adds it to the xenstore parameter
        record for the given vm instance. If the key exists in xenstore,
        it is overwritten
        """
        vm_ref = self._get_vm_opaque_ref(instance_or_vm)
        self.remove_from_param_xenstore(instance_or_vm, key)
        jsonval = json.dumps(val)
        self._session.call_xenapi_request('VM.add_to_xenstore_data',
                                          (vm_ref, key, jsonval))

    def write_to_param_xenstore(self, instance_or_vm, mapping):
        """
        Takes a dict and writes each key/value pair to the xenstore
        parameter record for the given vm instance. Any existing data for
        those keys is overwritten.
        """
        for k, v in mapping.iteritems():
            self.add_to_param_xenstore(instance_or_vm, k, v)

    def remove_from_param_xenstore(self, instance_or_vm, key_or_keys):
        """
        Takes either a single key or a list of keys and removes
        them from the xenstore parameter record data for the given VM.
        If the key doesn't exist, the request is ignored.
        """
        vm_ref = self._get_vm_opaque_ref(instance_or_vm)
        if isinstance(key_or_keys, basestring):
            keys = [key_or_keys]
        else:
            keys = key_or_keys
        for key in keys:
            self._session.call_xenapi_request('VM.remove_from_xenstore_data',
                                              (vm_ref, key))

    def clear_param_xenstore(self, instance_or_vm):
        """Removes all data from the xenstore parameter record for this VM."""
        self.write_to_param_xenstore(instance_or_vm, {})
    ########################################################################


class SimpleDH(object):
    """
    This class wraps all the functionality needed to implement
    basic Diffie-Hellman-Merkle key exchange in Python. It features
    intelligent defaults for the prime and base numbers needed for the
    calculation, while allowing you to supply your own. It requires that
    the openssl binary be installed on the system on which this is run,
    as it uses that to handle the encryption and decryption. If openssl
    is not available, a RuntimeError will be raised.
    """
    def __init__(self, prime=None, base=None, secret=None):
        """
        You can specify the values for prime and base if you wish;
        otherwise, reasonable default values will be used.
        """
        if prime is None:
            self._prime = 162259276829213363391578010288127
        else:
            self._prime = prime
        if base is None:
            self._base = 5
        else:
            self._base = base
        self._shared = self._public = None

        self._dh = M2Crypto.DH.set_params(
                self.dec_to_mpi(self._prime),
                self.dec_to_mpi(self._base))
        self._dh.gen_key()
        self._public = self.mpi_to_dec(self._dh.pub)

    def get_public(self):
        return self._public

    def compute_shared(self, other):
        self._shared = self.bin_to_dec(
                self._dh.compute_key(self.dec_to_mpi(other)))
        return self._shared

    def mpi_to_dec(self, mpi):
        bn = M2Crypto.m2.mpi_to_bn(mpi)
        hexval = M2Crypto.m2.bn_to_hex(bn)
        dec = int(hexval, 16)
        return dec

    def bin_to_dec(self, binval):
        bn = M2Crypto.m2.bin_to_bn(binval)
        hexval = M2Crypto.m2.bn_to_hex(bn)
        dec = int(hexval, 16)
        return dec

    def dec_to_mpi(self, dec):
        bn = M2Crypto.m2.dec_to_bn('%s' % dec)
        mpi = M2Crypto.m2.bn_to_mpi(bn)
        return mpi

    def _run_ssl(self, text, decrypt=False):
        cmd = ['openssl', 'aes-128-cbc', '-A', '-a', '-pass',
              'pass:%s' % self._shared, '-nosalt']
        if decrypt:
            cmd.append('-d')
        out, err = utils.execute(*cmd, process_input=text)
        if err:
            raise RuntimeError(_('OpenSSL error: %s') % err)
        return out

    def encrypt(self, text):
        return self._run_ssl(text).strip('\n')

    def decrypt(self, text):
        return self._run_ssl(text, decrypt=True)
