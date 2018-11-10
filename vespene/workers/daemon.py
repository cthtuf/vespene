#  Copyright 2018, Michael DeHaan LLC
#  License: Apache License Version 2.0 + Commons Clause
#  -------------------------------------------------------------------------
#  daemon.py - this is the main entry point for each worker process. It
#  doesn't fork. The build will periodically check to see if it is flagged
#  as one that should be stopped, and if so, will self terminate. Logic
#  is mostly in 'builder.py'.
#  --------------------------------------------------------------------------
from logging import getLogger
import time
import traceback
from datetime import datetime, timedelta
import sys

from django.db import transaction
from django.utils import timezone
from django.db import DatabaseError

from vespene.models.build import (ABORTED, ABORTING, ORPHANED, QUEUED, Build)
from vespene.models.organization import Organization
from vespene.models.worker_pool import WorkerPool
from vespene.workers.builder import BuildLord
from vespene.workers.scheduler import Scheduler
from vespene.workers.importer import ImportManager

LOG = getLogger(__name__)

FLAG_ABORTED_AFTER_ABORTING_MINUTES = 1


#==============================================================================

class Daemon(object):
    """
    Worker main loop.
    This doesn't have any daemonization code at the moment, it is expected you would run it from supervisor,
    wrapped by ssh-agent
    """

    # -------------------------------------------------------------------------

    def __init__(self, pool_name, max_wait_minutes=-1, max_builds=-1):
        """
        Create a worker that serves just one queue.
        """
        self.pool = pool_name
        self.max_wait_minutes = max_wait_minutes
        self.build_counter = max_builds
        self.reload()     
        self.ready_to_serve = False
        self.time_counter = datetime.now(tz=timezone.utc)

        LOG.info(f"Serving queue: self.pool")

    # -------------------------------------------------------------------------

    def reload(self):
        pools = WorkerPool.objects.filter(name=self.pool)
        if pools.count() != 1:
            LOG.error(f"Worker pool does not (yet?) exist: {self.pool}")
            self.pool_obj = None
        else:
            self.pool_obj = pools.first()

    # -------------------------------------------------------------------------

    def run(self):
        """
        Main loop.
        """

        while True:
            try:
                self.reload()
                if self.pool_obj is not None:
                    self.body()
            except Exception:
                traceback.print_exc()
            finally:
                if self.pool_obj is not None:
                    time.sleep(self.pool_obj.sleep_seconds)
                else:
                    time.sleep(60)

    # -------------------------------------------------------------------------

    def find_build(self):

        # try to run any build queued in the last interval <default: 1 hour>, abort all other builds 
        threshold = datetime.now(tz=timezone.utc) - timedelta(minutes=self.pool_obj.auto_abort_minutes)
        builds = Build.objects.filter(
            status = QUEUED,
            worker_pool__name = self.pool,
            queued_time__gt = threshold
        )
        count = builds.count() 
        if count == 0:
            return None
        first = builds.order_by('id').first()

        with transaction.atomic():
            try:
                first = Build.objects.select_for_update(nowait=True).get(id=first.pk)
            except DatabaseError:
                return None
            if count > 1 and self.pool_obj.build_latest:
                self.cleanup_extra_builds(first)
            return first

    # -------------------------------------------------------------------------

    def cleanup_extra_builds(self, first):
        rest = Build.objects.filter(
            status = QUEUED,
            project = first.project
        ).exclude(
            id = first.pk
        )
        rest.update(status=ABORTED)

    # -------------------------------------------------------------------------

    def cleanup_orphaned_builds(self):

        # builds that are queued for too long...
        threshold = datetime.now(tz=timezone.utc) - timedelta(minutes=self.pool_obj.auto_abort_minutes)
        orphaned = Build.objects.filter(
            status=QUEUED, 
            project__worker_pool__name = self.pool,
            queued_time__lt = threshold
        )
        for orphan in orphaned.all():
            LOG.warning(f"Build {orphan.id} was in queued status too long and not picked up by another worker, "
                        f"flagging as orphaned")

        orphaned.update(status=ORPHANED)

        # builds that haven't been aborted in too long for ANY worker pool
        threshold = datetime.now(tz=timezone.utc) - timedelta(minutes=FLAG_ABORTED_AFTER_ABORTING_MINUTES)
        orphaned = Build.objects.filter(
            status=ABORTING,
            queued_time__lt = threshold
        )
        for orphan in orphaned.all():
            LOG.warning(f"Build {orphan.id} was in aborting status too long, assuming successfully aborted")

        orphaned.update(status=ABORTED)

    # -------------------------------------------------------------------------

    def import_organizations(self):
        
        organizations = Organization.objects.filter(import_enabled=True, worker_pool=self.pool_obj)
        for org in organizations:
            with transaction.atomic():
                try:
                    org = organizations.select_for_update(nowait=True).get(pk=org.pk)
                    repo_importer = ImportManager(org)
                    repo_importer.do_import()  
                    org.save()
                except DatabaseError:
                    LOG.exception('Error white importing organilations')

    # -------------------------------------------------------------------------

    def schedule_builds(self):
        Scheduler().go()

    # -------------------------------------------------------------------------

    def body(self):
        """
        Main block, all exceptions are caught.
        """

        self.import_organizations()
        self.cleanup_orphaned_builds()
        self.schedule_builds()

        build = self.find_build()
        if build:
            self.time_counter = datetime.now(tz=timezone.utc)

            LOG.debug(f"Building: {build.id}, project: {build.project.name}")
            BuildLord(build).go()

            self.build_counter = self.build_counter - 1
            if self.build_counter == 0:
                LOG.debug("Requested max build count per worker limit reached, exiting")
                sys.exit(0)

        else:

            now = datetime.now(tz=timezone.utc)
            delta = now - self.time_counter
            if (self.max_wait_minutes > 0) and (delta.total_seconds() * 60 > self.max_wait_minutes):
                LOG.debug(f"No build has occured in {self.max_wait_minutes} minutes, exiting")
                sys.exit(0)