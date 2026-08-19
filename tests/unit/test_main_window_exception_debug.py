from pa_agent.gui.main_window import MainWindow


def test_network_error_debug_bundle_is_not_described_as_validation_failure():
    text = MainWindow._build_exception_debug_bundle(
        object(),
        {
            "type": "network_error",
            "stage": "stage1",
            "message": "Invalid max_tokens value",
        },
    )

    assert "模型/API 网关请求失败" in text
    assert "不属于阶段 JSON 校验错误" in text
    assert "阶段一校验失败时" not in text
