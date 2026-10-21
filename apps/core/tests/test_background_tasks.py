import threading
import time

from django.test import SimpleTestCase

from apps.core.tasks import run_in_background, wait_for_background_tasks


class BackgroundTaskJoinTests(SimpleTestCase):
    def test_wait_for_background_tasks_joins_started_workers(self):
        done = threading.Event()

        def work():
            time.sleep(0.05)
            done.set()

        run_in_background(work)
        wait_for_background_tasks(timeout=2.0)
        self.assertTrue(done.is_set())
