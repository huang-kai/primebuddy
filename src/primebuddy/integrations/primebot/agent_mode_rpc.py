"""在 SoC 0 的独立 ROS 进程中读写模式，避免 Agent 重启后的旧客户端失效。"""
import os
import json
import sys


def main():
    import rclpy
    from aimdk_msgs.msg import AgentPropertyIdType, AgentPropertiesValue
    from aimdk_msgs.srv import GetAgentPropertiesRequest, SetAgentPropertiesRequest

    rclpy.init()
    node = rclpy.create_node('primebuddy_agent_mode_' + str(os.getpid()))
    try:
        if AgentPropertyIdType.AGENT_PROPERTY_RUN_MODE != 2:
            raise RuntimeError('模式属性定义不匹配')

        def call(service, request):
            client = node.create_client(service, '/aimdk_5Fmsgs/srv/' + service.__name__)
            try:
                request.header.header.stamp = node.get_clock().now().to_msg()
                if not client.wait_for_service(timeout_sec=20):
                    raise RuntimeError('模式服务未就绪')
                future = client.call_async(request)
                rclpy.spin_until_future_complete(node, future, timeout_sec=15)
                if not future.done() or future.result() is None:
                    raise RuntimeError('模式请求超时')
                response = future.result()
                if response.header.header.code != 0:
                    raise RuntimeError('模式请求失败')
                return response
            finally:
                node.destroy_client(client)

        if len(sys.argv) == 2:
            request = SetAgentPropertiesRequest.Request()
            request.contents.properties = [AgentPropertiesValue(
                key=AgentPropertyIdType(value=2), value=sys.argv[1])]
            if call(SetAgentPropertiesRequest, request).header.status.value not in (0, 1):
                raise RuntimeError('模式写入失败')
        request = GetAgentPropertiesRequest.Request()
        request.property_ids = [AgentPropertyIdType(value=2)]
        response = call(GetAgentPropertiesRequest, request)
        if response.header.status.value != 1:
            raise RuntimeError('模式读取失败')
        mode = next(p.value for p in response.contents.properties if p.key.value == 2)
        if len(sys.argv) == 2 and mode != sys.argv[1]:
            raise RuntimeError('模式写入后读回不一致')
        print(json.dumps({'mode': mode}))
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
