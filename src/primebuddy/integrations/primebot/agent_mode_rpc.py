"""在 SoC 0 的独立 ROS 进程中读写模式，避免 Agent 重启后的旧客户端失效。"""
import os
import json
import sys
import time

DISCOVERY_SETTLE_SECONDS = 5.0


def settle_discovery(node, ros):
    # 服务出现在图中不代表 FastDDS 请求通道已完成发现；先 spin，再进行只读握手。
    deadline = time.monotonic() + DISCOVERY_SETTLE_SECONDS
    while time.monotonic() < deadline:
        ros.spin_once(node, timeout_sec=min(.1, max(0, deadline - time.monotonic())))


class ModeRpcError(RuntimeError):
    def __init__(self, service, reason):
        self.service, self.reason = service, reason
        super().__init__(service + ': ' + reason)


def main():
    import rclpy
    from aimdk_msgs.msg import AgentPropertyIdType, AgentPropertiesValue
    from aimdk_msgs.srv import GetAgentPropertiesRequest, SetAgentPropertiesRequest

    if len(sys.argv) > 2 or len(sys.argv) == 2 and sys.argv[1] not in ('normal', 'only_voice'):
        raise ModeRpcError('arguments', 'unsupported_mode')
    rclpy.init()
    node = rclpy.create_node('primebuddy_agent_mode_' + str(os.getpid()))
    clients = {}
    try:
        if AgentPropertyIdType.AGENT_PROPERTY_RUN_MODE != 2:
            raise RuntimeError('模式属性定义不匹配')

        services = [GetAgentPropertiesRequest]
        if len(sys.argv) == 2:
            services.append(SetAgentPropertiesRequest)
        for service in services:
            clients[service] = node.create_client(service, '/aimdk_5Fmsgs/srv/' + service.__name__)
        for service, client in clients.items():
            if not client.wait_for_service(timeout_sec=20):
                raise ModeRpcError(service.__name__, 'service_unavailable')
        settle_discovery(node, rclpy)

        def call(service, request):
            client = clients[service]
            request.header.header.stamp = node.get_clock().now().to_msg()
            future = client.call_async(request)
            rclpy.spin_until_future_complete(node, future, timeout_sec=15)
            if not future.done():
                raise ModeRpcError(service.__name__, 'response_timeout')
            response = future.result()
            if response is None:
                raise ModeRpcError(service.__name__, 'empty_response')
            if response.header.header.code != 0:
                raise ModeRpcError(service.__name__, 'response_code_' + str(response.header.header.code))
            return response

        def read_mode():
            request = GetAgentPropertiesRequest.Request()
            request.property_ids = [AgentPropertyIdType(value=2)]
            response = call(GetAgentPropertiesRequest, request)
            if response.header.status.value != 1:
                raise ModeRpcError(GetAgentPropertiesRequest.__name__, 'read_rejected')
            values = [p.value for p in response.contents.properties if p.key.value == 2]
            if len(values) != 1 or values[0] not in ('normal', 'only_voice'):
                raise ModeRpcError(GetAgentPropertiesRequest.__name__, 'invalid_mode')
            return values[0]

        write_response_timeout = False
        if len(sys.argv) == 2:
            # 只读握手失败时不写模式；SET 始终最多一次。
            read_mode()
            request = SetAgentPropertiesRequest.Request()
            request.contents.properties = [AgentPropertiesValue(
                key=AgentPropertyIdType(value=2), value=sys.argv[1])]
            try:
                response = call(SetAgentPropertiesRequest, request)
            except ModeRpcError as error:
                if error.reason != 'response_timeout':
                    raise
                # 写入可能已生效；不重发写请求，用已完成发现的只读客户端核对实际模式。
                write_response_timeout = True
            else:
                if response.header.status.value not in (0, 1):
                    raise ModeRpcError(SetAgentPropertiesRequest.__name__, 'write_rejected')
        mode = read_mode()
        if len(sys.argv) == 2 and mode != sys.argv[1]:
            raise ModeRpcError(GetAgentPropertiesRequest.__name__,
                'readback_mismatch expected=' + sys.argv[1] + ' actual=' + mode
                + ' write_response_timeout=' + str(write_response_timeout))
        print(json.dumps({'mode': mode, 'write_response_timeout': write_response_timeout}))
    finally:
        for client in clients.values():
            node.destroy_client(client)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    try:
        main()
    except ModeRpcError as error:
        print(json.dumps({'error': {'service': error.service, 'reason': error.reason}}))
        raise SystemExit(1)
