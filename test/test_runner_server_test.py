import threading
import tornado.ioloop

from discovery_failure_test import BrokenImportTestCase
from testify import assert_equal, class_setup, setup, teardown, test_case, test_runner_server
from testify.test_logger import _log
from testify.utils import turtle


def get_test(server, runner_id):
    """A blocking function to request a test from a TestRunnerServer."""
    sem = threading.Semaphore(0)
    tests_received = [] # Python closures aren't as cool as JS closures, so we have to use something already on the heap in order to pass data from an inner func to an outer func.

    def inner(test_dict):
        tests_received.append(test_dict)
        sem.release()

    def inner_empty():
        tests_received.append(None)
        sem.release()

    server.get_next_test(runner_id, inner, inner_empty)
    sem.acquire()

    (test_received,) = tests_received
    return test_received

class TestRunnerServerBaseTestCase(test_case.TestCase):
    __test__ = False

    def build_test_case(self):
        class DummyTestCase(test_case.TestCase):
            def __init__(self_, *args, **kwargs):
                super(DummyTestCase, self_).__init__(*args, **kwargs)
                self_.should_pass = kwargs.pop('should_pass', True)
            def test(self_):
                assert self_.should_pass

        self.dummy_test_case = DummyTestCase

    def start_server(self, test_reporters=None):
        if test_reporters is None:
            test_reporters = []

        self.server = test_runner_server.TestRunnerServer(
            self.dummy_test_case,
            options=turtle.Turtle(
                runner_timeout=1,
                server_timeout=10,
                revision=None,
                shutdown_delay_for_connection_close=0.001,
                shutdown_delay_for_outstanding_runners=1,
            ),
            serve_port=0,
            test_reporters=test_reporters,
            plugin_modules=[],
        );

        def catch_exceptions_in_thread():
            try:
                self.server.run()
            except (Exception, SystemExit), exc:
                _log.error("Thread threw exception: %r" % exc)
                raise

        self.thread = threading.Thread(None, catch_exceptions_in_thread)
        self.thread.start()

    def stop_server(self):
        self.server.shutdown()
        self.thread.join()

    @class_setup
    def setup_test_case(self):
        self.build_test_case()

    @setup
    def setup_server(self):
        self.start_server()

    @teardown
    def teardown_server(self):
        self.stop_server()


class TestRunnerServerBrokenImportTestCase(TestRunnerServerBaseTestCase, BrokenImportTestCase,):
    def create_broken_import_file(self):
        """We must control when this setup method is run since
        build_test_case() depends on it. So we'll stub it out for now and call
        it when we're ready from build_test_case()."""
        pass

    def build_test_case(self):
        super(TestRunnerServerBrokenImportTestCase, self).create_broken_import_file()
        self.dummy_test_case = self.broken_import_module

    def start_server(self):
        """To insure the server has started before we start testing, set up a
        lock which will be released when reporting happens as the final phase
        of server startup.

        Without this, weird race conditions abound where things break because
        server startup is incomplete."""
        lock = threading.Event()
        self.report_call_count = 0

        def report_releases_lock():
            lock.set()
            self.report_call_count += 1
        self.mock_reporter = turtle.Turtle(report=report_releases_lock)
        super(TestRunnerServerBrokenImportTestCase, self).start_server(test_reporters=[self.mock_reporter])

        lock.wait(1)
        assert lock.isSet(), "Timed out waiting for server to finish starting."

    def test_reports_are_generated_after_discovery_failure(self):
        assert_equal(self.report_call_count, 1)


class TestRunnerServerTestCase(TestRunnerServerBaseTestCase):
    def timeout_class(self, runner, test):
        assert test
        tornado.ioloop.IOLoop.instance().add_callback(lambda: self.server.check_in_class(runner, test['class_path'], timed_out=True))

    def run_test(self, runner_id, should_pass=True):
        test_instance = self.dummy_test_case(should_pass=should_pass)
        test_instance.register_callback(
            test_case.TestCase.EVENT_ON_COMPLETE_TEST_METHOD,
            lambda result: self.server.report_result(runner_id, result)
        )
        test_instance.run()

    def test_passing_tests_run_only_once(self):
        """Start a server with one test case to run. Make sure it hands out that test, report it as success, then make sure it gives us nothing else."""
        first_test = get_test(self.server, 'runner1')

        assert_equal(first_test['class_path'], 'test.test_runner_server_test DummyTestCase')
        assert_equal(first_test['methods'], ['test'])

        self.run_test('runner1')

        second_test = get_test(self.server, 'runner1')
        assert_equal(second_test, None)

    def test_requeue_on_failure(self):
        """Start a server with one test case to run. Make sure it hands out that test, report it as failure, then make sure it gives us the same one, then nothing else."""
        first_test = get_test(self.server, 'runner1')
        assert_equal(first_test['class_path'], 'test.test_runner_server_test DummyTestCase')
        assert_equal(first_test['methods'], ['test'])

        self.run_test('runner1', should_pass=False)

        second_test = get_test(self.server, 'runner2')
        assert_equal(second_test['class_path'], 'test.test_runner_server_test DummyTestCase')
        assert_equal(second_test['methods'], ['test'])

        self.run_test('runner2', should_pass=False)

        assert_equal(get_test(self.server, 'runner3'), None)

    def test_requeue_on_timeout(self):
        """Start a server with one test case to run. Make sure it hands out the same test twice, then nothing else."""

        first_test = get_test(self.server, 'runner1')
        self.timeout_class('runner1', first_test)

        # Now just ask for a second test. This should give us the same test again.
        second_test = get_test(self.server, 'runner2')
        self.timeout_class('runner2', second_test)

        # Ask for a third test. This should give us None.
        third_test = get_test(self.server, 'runner3')

        assert first_test
        assert second_test

        assert_equal(first_test['class_path'], second_test['class_path'])
        assert_equal(first_test['methods'], second_test['methods'])
        assert_equal(third_test, None)

    def test_fail_then_timeout_twice(self):
        """Fail, then time out, then time out again, then time out again.
        The first three fetches should give the same test; the last one should be None."""
        first_test = get_test(self.server, 'runner1')
        self.run_test('runner1', should_pass=False)

        second_test = get_test(self.server, 'runner2')
        self.timeout_class('runner2', second_test)

        third_test = get_test(self.server, 'runner3')
        self.timeout_class('runner3', third_test)


        assert_equal(first_test['class_path'], second_test['class_path'])
        assert_equal(first_test['methods'], second_test['methods'])

        assert_equal(first_test['class_path'], third_test['class_path'])
        assert_equal(first_test['methods'], third_test['methods'])

        # Check that it didn't requeue again.
        assert_equal(get_test(self.server, 'runner4'), None)

    def test_timeout_then_fail_twice(self):
        """Time out once, then fail, then fail again.
        The first three fetches should give the same test; the last one should be None."""
        first_test = get_test(self.server, 'runner1')
        self.timeout_class('runner1', first_test)

        # Don't run it.
        second_test = get_test(self.server, 'runner2')
        self.run_test('runner2', should_pass=False)
        third_test = get_test(self.server, 'runner3')
        self.run_test('runner3', should_pass=False)
        assert_equal(first_test['class_path'], second_test['class_path'])
        assert_equal(first_test['methods'], second_test['methods'])
        assert_equal(first_test['class_path'], third_test['class_path'])
        assert_equal(first_test['methods'], third_test['methods'])

        # Check that it didn't requeue again.
        assert_equal(get_test(self.server, 'runner4'), None)

    def test_get_next_test_doesnt_loop_forever(self):
        """In certain situations, get_next_test will recurse until it runs out of stack space. Make sure that doesn't happen.

        Here are the conditions needed to reproduce this bug
         - The server sees multiple runners
         - The server has more than one test in its queue
         - All the tests in the server's queue were last run by the runner asking for tests.
        """
        self.server.test_queue = test_runner_server.AsyncDelayedQueue()

        self.server.test_queue.put(0, {'last_runner': 'foo', 'class_path': '1', 'methods': ['blah'], 'fixture_methods': []})
        self.server.test_queue.put(0, {'last_runner': 'foo', 'class_path': '2', 'methods': ['blah'], 'fixture_methods': []})
        self.server.test_queue.put(0, {'last_runner': 'foo', 'class_path': '3', 'methods': ['blah'], 'fixture_methods': []})

        failures = []

        def on_test_callback(test):
            failures.append("get_next_test called back with a test.")

        def on_empty_callback():
            failures.append("get_next_test called back with no test.")

        # We need the server to see multiple runners, otherwise the offending code path doesn't get triggered.
        get_test(self.server, 'bar')
        # If this test fails the way we expect it to, this call to get_test will block indefinitely.

        thread = threading.Thread(None, lambda: self.server.get_next_test('foo', on_test_callback, on_empty_callback))
        thread.start()
        thread.join(0.5)

        assert not thread.is_alive(), "get_next_test is still running after 0.5s"

        if failures:
            raise Exception(' '.join(failures))

# vim: set ts=4 sts=4 sw=4 et:
