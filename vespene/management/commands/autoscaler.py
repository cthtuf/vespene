#  Copyright 2018, Michael DeHaan LLC
#  License: Apache License Version 2.0 + Commons Clause
#  -----------------------------------------------------
#  runs autoscaling plugins to dynamically size worker pools

from logging import getLogger
import time
import traceback
import subprocess
import sys
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.utils import timezone

from vespene.models.worker_pool import WorkerPool
from vespene.models.build import Build
from vespene.common.plugin_loader import PluginLoader

LOG = getLogger(__name__)

class Command(BaseCommand):
    help = 'Runs autoscaling logic for one or more configured worker pools'

    def add_arguments(self, parser):
        parser.add_argument('--queue', action='append', type=str, help='name of the queue, use \'general\' for the unassigned queue')
        parser.add_argument('--sleep', type=int, help='how long to sleep between checks (in seconds)', default=20)
        parser.add_argument('--force', action='store_true', help='ignore timers and run the detector, then exit')

    def get_worker_pool(self, pool_name):
        
        worker_pools = WorkerPool.objects.filter(name=pool_name)
        if not worker_pools.exists():
            LOG.info("Worker pool does not exist: {pool_name}")
            return None
        return worker_pools.first()

    def handle_pool(self, worker_pool, planner, executor, force):

        if worker_pool is None:
            LOG.warning(f"There is no worker pool named {worker_pool} yet")
            # probably a provisioning order issue, this will degrade performance but should not be fatal
            # just avoid hammering the system until it exists
            time.sleep(60)
            return

        if not worker_pool.autoscaling_enabled:
            return

        now = datetime.now(tz=timezone.utc)
        autoscale_status = 0
        last_autoscaled = worker_pool.last_autoscaled

        try:
            if not (force or planner.is_time_to_adjust(worker_pool)):
                return

            parameters = planner.get_parameters(worker_pool)
            LOG.debug(f"Autoscaling parameters: {parameters} for {worker_pool.name}")
            
            result = executor.scale_worker_pool(worker_pool, parameters)
            
            LOG.info(f"Autoscaling success for {worker_pool.name}")
            last_autoscaled = datetime.now(tz=timezone.utc)

        except subprocess.CalledProcessError as cpe:
            LOG.exception("Autoscaling failed, return code: {cpe.returncode}")
            autoscale_status = cpe.returncode
        except:
            LOG.exception("Autoscaling failed for {worker_pool.name}")
            autoscale_status = 1

        finally:
            WorkerPool.objects.filter(
                pk=worker_pool.pk
            ).update(last_autoscaled=last_autoscaled, autoscaling_status=autoscale_status)

    def handle(self, *args, **options):
       
        worker_pools = options.get('queue')
        sleep_time = options.get('sleep')
        force = options.get('force')

        LOG.info("Started...")

        self.plugin_loader = PluginLoader()
        self.planner_plugins = self.plugin_loader.get_autoscaling_planner_plugins()
        self.executor_plugins = self.plugin_loader.get_autoscaling_executor_plugins()

        while True:

            for pool in worker_pools:
                worker_pool = self.get_worker_pool(pool)
                if worker_pool is None:
                    continue
                planner = self.planner_plugins[worker_pool.planner]
                executor = self.executor_plugins[worker_pool.executor]
                self.handle_pool(worker_pool, planner, executor, force)
                time.sleep(1)

            if force:
                break
            else:
                time.sleep(sleep)

        LOG.info("Exited...")

