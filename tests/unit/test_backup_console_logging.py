"""Console wording must be neutral without destroying file diagnostics."""
import io
import logging
import pytest
from pa_agent.util.logging import ConsoleFormatter, MaskingFormatter, _LOG_FORMAT


@pytest.mark.parametrize('name,message', [
    ('pa_agent.ai.sensenova_fallback', 'SenseNova 主接口失败，已由备用模型接管: provider=kilo model=inclusionai/ling-3.1-flash (30000 ms)'),
    ('pa_agent.ai.sensenova_fallback', 'SenseNova free fallback miss: kilo/nvidia/nemotron:free (https://api.kilo.ai 429)'),
    ('pa_agent.ai.sensenova_fallback', 'Kilo free listing failed: API unreachable'),
    ('pa_agent.ai.sensenova_fallback', 'SenseNova free fallback validation failed: kilo/model:free (schema)'),
    ('pa_agent.orchestrator.two_stage', 'SenseNova free fallback triggered for stage1 reason=free/model'),
    ('pa_agent.orchestrator.two_stage', 'SenseNova free fallback exhausted for stage2 | attempts: kilo/free/model'),
    ('pa_agent.orchestrator.two_stage', 'SenseNova free fallback aborted with error: free/model'),
])
def test_console_is_neutral_and_file_keeps_original(name, message):
    record = logging.LogRecord(name, logging.WARNING, __file__, 1, '%s', (message,), None)
    console = io.StringIO()
    disk = io.StringIO()
    console_handler = logging.StreamHandler(console)
    console_handler.setFormatter(ConsoleFormatter(_LOG_FORMAT))
    file_handler = logging.StreamHandler(disk)
    file_handler.setFormatter(MaskingFormatter(_LOG_FORMAT))
    # Check both dispatch orders, since handlers share the same record.
    for handlers in ((console_handler, file_handler), (file_handler, console_handler)):
        console.seek(0); console.truncate(0)
        disk.seek(0); disk.truncate(0)
        for handler in handlers:
            handler.handle(record)
        public = console.getvalue()
        for forbidden in ('免费', 'free', 'kilo', 'ling-', 'nemotron', 'fallback', 'api.kilo.ai'):
            assert forbidden not in public
        assert message in disk.getvalue()
        assert record.getMessage() == message
        assert record.name == name


def test_non_backup_error_keeps_its_meaning_and_masks_key():
    record = logging.LogRecord('pa_agent.gui.main_window', logging.ERROR, __file__, 1,
                               'Connection failed: %s', ('secret-key-test',), None)
    rendered = ConsoleFormatter(_LOG_FORMAT, api_key='secret-key-test').format(record)
    assert 'Connection failed' in rendered
    assert 'secret-key-test' not in rendered
