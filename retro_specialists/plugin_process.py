"""Bound subprocess bytes while it runs; input and argv come from trusted host code."""
import os
import selectors
import signal
import subprocess
import tempfile
import time


class WorkerLimitError(RuntimeError):
    pass


def _kill_reap(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=5)


def bounded_run(argv, request, timeout, max_bytes):
    if len(request) > 2_000_000 or not 0 < timeout <= 120 or not 1 <= max_bytes <= 8_000_000:
        raise ValueError('Invalid worker bounds')
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as input_file:
        input_file.write(request); input_file.seek(0)
        child = subprocess.Popen(argv, stdin=input_file, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
            start_new_session=True)
        output, errors = bytearray(), bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, output)
                selector.register(child.stderr, selectors.EVENT_READ, errors)
                total = 0
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WorkerLimitError('timeout')
                    for key, _ in selector.select(min(remaining, .1)):
                        data = os.read(key.fd, 8192)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(data)
                        if total > max_bytes:
                            raise WorkerLimitError('output_limit')
                        key.data.extend(data)
                try:
                    code = child.wait(timeout=max(.001, deadline-time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise WorkerLimitError('timeout') from exc
            return code, bytes(output), bytes(errors)
        except BaseException:
            _kill_reap(child)
            raise
        finally:
            child.stdout.close(); child.stderr.close()
