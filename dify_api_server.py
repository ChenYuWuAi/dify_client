#!/usr/bin/python
import json
import requests
import uuid
import re
from flask import Flask, request, Response, jsonify
from urllib3.exceptions import InsecureRequestWarning

# 禁用SSL警告（仅用于测试环境）
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

app = Flask(__name__)

class ThinkTagConverter:
    """
    状态转换器：
      - 固定的开始标签为：
        '<details style="color:gray;background-color: #f8f8f8;padding: 8px;border-radius: 4px;" open> <summary> 思考中... </summary>'
      - 结束标签为：'</details>'
    处理逻辑：
      1. 非思考模式时，查找 start_marker，一旦找到就：
         - 输出 start_marker 之前的内容；
         - 立即输出 "<think>" 并进入思考模式；
         - 剩余部分进入缓冲区。
      2. 思考模式中：
         - 检查缓冲区中是否存在完整的 end_marker，如果存在，则输出其前的内容、输出 "</think>" 并退出思考模式。
         - 如果没有完整的 end_marker，则检查缓冲区末尾是否可能为 end_marker 的部分前缀，
           若有则保留这部分候选内容，其余部分立即输出；若没有则全部输出。
      3. flush() 时，无论是否处于思考模式，都输出剩余内容，并重置状态。
    """
    def __init__(self):
        self.buffer = ""
        self.in_think = False
        self.start_marker = '<details style="color:gray;background-color: #f8f8f8;padding: 8px;border-radius: 4px;" open> <summary> 思考中... </summary>'
        self.end_marker = '</details>'

    def _longest_suffix_candidate(self, s, marker):
        """
        返回 s 的末尾部分，该部分是 marker 的前缀（候选闭合标签），长度越长越好
        """
        candidate = ""
        max_len = min(len(s), len(marker))
        for i in range(1, max_len + 1):
            if s[-i:] == marker[:i]:
                candidate = s[-i:]
        return candidate

    def process(self, text):
        """
        增量处理输入的文本：
          - 如果未进入思考模式，则查找 start_marker，找到后输出前面内容和 <think> 标签；
          - 如果已进入思考模式，则实时输出缓冲中除可能为 end_marker 候选外的内容，
            并在检测到完整 end_marker 时输出 "</think>"。
        """
        self.buffer += text
        output = ""

        # 未进入思考模式时：查找 start_marker
        if not self.in_think:
            idx = self.buffer.find(self.start_marker)
            if idx != -1:
                # 输出 start_marker 前的内容
                output += self.buffer[:idx]
                # 接收到完整的 start_marker后，立即输出 <think> 标签，进入思考模式
                output += "<think>"
                # 去掉 start_marker 部分
                self.buffer = self.buffer[idx + len(self.start_marker):]
                self.in_think = True
            else:
                # 没有找到 start_marker，直接输出全部内容，并清空缓冲
                output += self.buffer
                self.buffer = ""
                return output

        # 如果处于思考模式中
        if self.in_think:
            idx = self.buffer.find(self.end_marker)
            if idx != -1:
                # 如果找到完整的 end_marker，
                # 则输出 end_marker 前的内容，并输出闭合标签 "</think>"，
                # 并退出思考模式，将 end_marker 后的内容保留在缓冲区继续处理
                output += self.buffer[:idx]
                output += "</think>"
                self.buffer = self.buffer[idx + len(self.end_marker):]
                self.in_think = False
            else:
                # 没有完整的 end_marker
                # 检查缓冲末尾是否可能为 end_marker 的前缀（候选）
                candidate = self._longest_suffix_candidate(self.buffer, self.end_marker)
                if candidate:
                    # 输出除候选部分以外的内容
                    if len(self.buffer) > len(candidate):
                        output += self.buffer[:-len(candidate)]
                        self.buffer = self.buffer[-len(candidate):]
                    # 否则整个缓冲都是候选，则不输出，等待后续补全
                else:
                    # 如果缓冲区里根本没有类似闭合标签的候选，则全部输出
                    output += self.buffer
                    self.buffer = ""
        return output

    def flush(self):
        """
        流结束时调用，输出剩余缓冲内容（如果处于思考模式，则不会补上闭合标签）
        并重置状态，确保下次新连接时状态干净。
        """
        out = self.buffer
        self.buffer = ""
        self.in_think = False
        return out


# 修改后的流处理函数，使用状态转换器
def process_stream_text(text, converter):
    """
    对传入文本使用 converter 处理，并返回可输出的部分
    """
    return converter.process(text)

class ChatClient:
    def __init__(self):
        self.api_url = "https://deepseektest1.seu.edu.cn/api/chat-messages"
        self.headers = {
            "Authorization": "Bearer #### 请在这里粘贴你的 API 密钥",
            "Content-Type": "application/json"
        }
        self.conversation_id = None
        self.parent_message_id = None

    def reset_conversation(self):
        """重置对话标识，开始新话题"""
        self.conversation_id = None
        self.parent_message_id = None

    def _process_event(self, event_data):
        """处理不同事件类型"""
        event_type = event_data.get('event')
        if event_type == 'workflow_started':
            self.conversation_id = event_data.get('conversation_id')

    def reset_chat_response(self):
        """重置对话响应，回复：对话已重置"""
        chunk = {
            "id": "chatcmpl-" + str(uuid.uuid4()),
            "object": "chat.completion.chunk",
            "model": "gpt-4o-mini",
            "choices": [{
                "delta": {"content": "对话已重置"},
                "index": 0,
                "finish_reason": None
            }]
        }
        yield "data: " + json.dumps(chunk) + "\n\n"
        yield "data: [DONE]\n\n"

    def stream_chat_response(self, query, model):
        """
        调用后端接口获取流式响应，并转换为 OpenAI 标准的 streaming 格式。
        利用状态转换器处理分散返回的思考过程，将完整的 <details> 块转换为 <think> 块输出。
        """
        payload = {
            "response_mode": "streaming",
            "conversation_id": self.conversation_id,
            "files": [],
            "query": query,
            "inputs": {},
            "parent_message_id": self.parent_message_id
        }
        # 实例化转换器，状态在整个流过程中保持
        converter = ThinkTagConverter()

        try:
            with requests.post(
                self.api_url,
                headers=self.headers,
                json=payload,
                stream=True,
                verify=False
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8')
                        if decoded_line.startswith('data:'):
                            json_str = decoded_line[len('data:'):].strip()
                            try:
                                event_data = json.loads(json_str)
                                self._process_event(event_data)
                                if event_data.get('event') == 'message':
                                    # 对流式片段进行状态处理转换
                                    fragment = event_data.get('answer', '')
                                    processed_fragment = process_stream_text(fragment, converter)
                                    # 更新对话状态
                                    self.conversation_id = event_data.get('conversation_id')
                                    self.parent_message_id = event_data.get('message_id')
                                    chunk = {
                                        "id": "chatcmpl-" + str(uuid.uuid4()),
                                        "object": "chat.completion.chunk",
                                        "model": model,
                                        "choices": [{
                                            "delta": {"content": processed_fragment},
                                            "index": 0,
                                            "finish_reason": None
                                        }]
                                    }
                                    yield "data: " + json.dumps(chunk) + "\n\n"
                                elif event_data.get('event') == 'message_end':
                                    self.parent_message_id = event_data.get('message_id')
                            except json.JSONDecodeError:
                                continue
                # 流结束时，刷新转换器内未输出的残留内容
                remaining = converter.flush()
                if remaining:
                    chunk = {
                        "id": "chatcmpl-" + str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{
                            "delta": {"content": remaining},
                            "index": 0,
                            "finish_reason": None
                        }]
                    }
                    yield "data: " + json.dumps(chunk) + "\n\n"
            yield "data: [DONE]\n\n"
        except requests.exceptions.RequestException as e:
            error_chunk = {
                "id": "chatcmpl-" + str(uuid.uuid4()),
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{
                    "delta": {"content": f"\nRequest failed: {str(e)}"},
                    "index": 0,
                    "finish_reason": "error"
                }]
            }
            yield "data: " + json.dumps(error_chunk) + "\n\n"
            yield "data: [DONE]\n\n"

    def chat(self, query, model):
        """
        非流式调用，将所有返回内容拼接后再一次性返回。
        为确保转换效果，这里先将所有文本拼接后统一用转换器处理。
        """
        payload = {
            "response_mode": "streaming",
            "conversation_id": self.conversation_id,
            "files": [],
            "query": query,
            "inputs": {},
            "parent_message_id": self.parent_message_id
        }

        full_response = ""
        try:
            with requests.post(
                self.api_url,
                headers=self.headers,
                json=payload,
                stream=True,
                verify=False
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line:
                        decoded_line = line.decode('utf-8')
                        if decoded_line.startswith('data:'):
                            json_str = decoded_line[len('data:'):].strip()
                            try:
                                event_data = json.loads(json_str)
                                self._process_event(event_data)
                                if event_data.get('event') == 'message':
                                    full_response += event_data.get('answer', '')
                                    self.conversation_id = event_data.get('conversation_id')
                                    self.parent_message_id = event_data.get('message_id')
                                elif event_data.get('event') == 'message_end':
                                    self.parent_message_id = event_data.get('message_id')
                            except json.JSONDecodeError:
                                continue
            # 使用转换器处理完整响应文本
            converter = ThinkTagConverter()
            processed_text = converter.process(full_response) + converter.flush()
            result = {
                "id": "chatcmpl-" + str(uuid.uuid4()),
                "object": "chat.completion",
                "model": model,
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": processed_text
                    },
                    "index": 0,
                    "finish_reason": "stop"
                }]
            }
            return result
        except requests.exceptions.RequestException as e:
            return {"error": f"Request failed: {str(e)}"}

# 全局单例（如果需要多用户支持，则需要为每个会话维护独立状态）
client = ChatClient()

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    req_data = request.get_json(force=True)
    model = req_data.get("model", "o3-mini")
    stream_flag = req_data.get("stream", False)
    messages = req_data.get("messages", [])

    # 检查是否重置对话
    if messages and messages[-1].get("content") == "clear":
        client.reset_conversation()
        return Response(client.reset_chat_response(), mimetype="text/event-stream")

    # 从 messages 中取最后一条用户消息作为查询
    user_query = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_query = msg.get("content", "")
            break

    if not user_query:
        return jsonify({"error": "没有提供用户消息"}), 400

    if stream_flag:
        return Response(client.stream_chat_response(user_query, model), mimetype="text/event-stream")
    else:
        result = client.chat(user_query, model)
        return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
