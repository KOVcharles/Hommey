from runtime import _generate_kwargs


def test_model_studio_deepseek_v4_disables_thinking_by_default():
    kwargs = _generate_kwargs({
        "model_name": "deepseek-v4-flash",
        "base_url": "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "temperature": 0.7,
        "max_tokens": 8192,
        "enable_thinking": False,
    })

    assert kwargs["extra_body"] == {"enable_thinking": False}


def test_non_model_studio_endpoints_do_not_receive_vendor_parameters():
    kwargs = _generate_kwargs({
        "model_name": "deepseek-v4-flash",
        "base_url": "https://api.example.com/v1",
        "enable_thinking": False,
    })

    assert "extra_body" not in kwargs
