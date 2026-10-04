"""
AgentScope Configuration for Hommey Multi-Agent Travel Planning System
适配 AgentScope 1.0.16+
"""
import agentscope

def init_agentscope():
    """
    初始化AgentScope

    注意：AgentScope 1.0.16+ 版本的API已改变：
    - init()函数不再接受model_configs参数
    - 模型配置改为直接在Agent初始化时指定
    """
    agentscope.init(
        project="Hommey-Travel-Planning",
        name="multi_agent_system",
        logging_level="INFO"
    )

    print(f"✓ AgentScope initialized (version: {agentscope.__version__})")
