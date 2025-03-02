#!/usr/bin/python
import json
import requests
import uuid
from flask import Flask, request, Response, jsonify
from urllib3.exceptions import InsecureRequestWarning

# 禁用 SSL 警告（仅用于测试环境）
requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)

app = Flask(__name__)

class ThinkTagConverter:
    """
    状态转换器：将 <details> 标签转换为 <think> 标签
    """
    def __init__(self):
        self.buffer = ""
        self.in_think = False
        self.start_marker = '<details style="color:gray;background-color: #f8f8f8;padding: 8px;border-radius: 4px;" open> <summary> 思考中... </summary>'
        self.end_marker = '</details>'

    def _longest_suffix_candidate(self, s, marker):
        """返回 s 的末尾部分，该部分是 marker 的前缀，长度越长越好"""
        candidate = ""
        max_len = min(len(s), len(marker))
        for i in range(1, max_len + 1):
            if s[-i:] == marker[:i]:
                candidate = s[-i:]
        return candidate

    def process(self, text):
        """增量处理输入文本"""
        self.buffer += text
        output = ""
        if not self.in_think:
            idx = self.buffer.find(self.start_marker)
            if idx != -1:
                output += self.buffer[:idx]
                output += "<think>"
                self.buffer = self.buffer[idx + len(self.start_marker):]
                self.in_think = True
            else:
                output += self.buffer
                self.buffer = ""
                return output

        if self.in_think:
            idx = self.buffer.find(self.end_marker)
            if idx != -1:
                output += self.buffer[:idx]
                output += "</think>"
                self.buffer = self.buffer[idx + len(self.end_marker):]
                self.in_think = False
            else:
                candidate = self._longest_suffix_candidate(self.buffer, self.end_marker)
                if candidate and len(self.buffer) > len(candidate):
                    output += self.buffer[:-len(candidate)]
                    self.buffer = self.buffer[-len(candidate):]
                else:
                    output += self.buffer
                    self.buffer = ""
        return output

    def flush(self):
        """流结束时输出剩余内容并重置状态"""
        out = self.buffer
        self.buffer = ""
        self.in_think = False
        return out

def process_stream_text(text, converter):
    """使用转换器处理流式文本"""
    return converter.process(text)

class ChatClient:
    def __init__(self):
        self.api_url = "https://deepseektest1.seu.edu.cn/api/chat-messages"
        self.stop_url_template = "https://deepseektest1.seu.edu.cn/api/chat-messages/{}/stop"
        self.headers = {
            "Authorization": "Bearer #### 请在这里粘贴你的 API 密钥",
            "Content-Type": "application/json"
        }
        self.conversation_id = None
        self.parent_message_id = None
        self.current_message_id = None  # 保存当前消息 ID 用于停止请求
        self.running = False

    def reset_conversation(self):
        """重置对话标识"""
        # 如果有正在运行的对话，发送停止请求
        if self.current_message_id:
            self.running = False
            self.send_stop_request()
            
        self.conversation_id = None
        self.parent_message_id = None
        self.current_message_id = None

    def _process_event(self, event_data):
        """处理事件类型"""
        event_type = event_data.get('event')
        if event_type == 'workflow_started' and self.running:
            self.conversation_id = event_data.get('conversation_id')
        elif event_type == 'message' and self.running:
            self.current_message_id = event_data.get('message_id')

    def reset_chat_response(self):
        """重置对话响应"""
        chunk = {
            "id": "chatcmpl-" + str(uuid.uuid4()),
            "object": "chat.completion.chunk",
            "model": "gpt-4o-mini",
            "choices": [{"delta": {"content": "对话已重置"}, "index": 0, "finish_reason": None}]
        }
        yield "data: " + json.dumps(chunk) + "\n\n"
        yield "data: [DONE]\n\n"

    def send_stop_request(self):
        """向后端发送停止请求"""
        print("Sending stop request...")
        if self.current_message_id:
            stop_url = self.stop_url_template.format(self.current_message_id)
            try:
                requests.post(stop_url, headers=self.headers, json={}, verify=False)
            except requests.exceptions.RequestException as e:
                print(f"Failed to send stop request: {str(e)}")

    def stream_chat_response(self, query, model):
        """流式聊天响应，处理客户端断开连接"""
        payload = {
            "response_mode": "streaming",
            "conversation_id": self.conversation_id,
            "files": [],
            "query": query,
            "inputs": {},
            "parent_message_id": self.parent_message_id
        }
        converter = ThinkTagConverter()

        self.running = True
        try:
            with requests.post(self.api_url, headers=self.headers, json=payload, stream=True, verify=False) as response:
                if(not self.running):
                    response.close()
                    return
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
                                    fragment = event_data.get('answer', '')
                                    processed_fragment = process_stream_text(fragment, converter)
                                    if self.running:
                                        self.conversation_id = event_data.get('conversation_id')
                                        self.parent_message_id = event_data.get('message_id')
                                    chunk = {
                                        "id": "chatcmpl-" + str(uuid.uuid4()),
                                        "object": "chat.completion.chunk",
                                        "model": model,
                                        "choices": [{"delta": {"content": processed_fragment}, "index": 0, "finish_reason": None}]
                                    }
                                    yield "data: " + json.dumps(chunk) + "\n\n"
                                elif event_data.get('event') == 'message_end' and self.running:
                                    self.parent_message_id = event_data.get('message_id')
                            except json.JSONDecodeError:
                                continue
                remaining = converter.flush()
                if remaining:
                    chunk = {
                        "id": "chatcmpl-" + str(uuid.uuid4()),
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"delta": {"content": remaining}, "index": 0, "finish_reason": None}]
                    }
                    yield "data: " + json.dumps(chunk) + "\n\n"
                yield "data: [DONE]\n\n"
        except requests.exceptions.RequestException as e:
            error_chunk = {
                "id": "chatcmpl-" + str(uuid.uuid4()),
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"delta": {"content": f"\nRequest failed: {str(e)}"}, "index": 0, "finish_reason": "error"}]
            }
            yield "data: " + json.dumps(error_chunk) + "\n\n"
            yield "data: [DONE]\n\n"
        except GeneratorExit:
            response.close()
            # 客户端断开连接，主动停止
            self.send_stop_request()  # 发送停止请求
            raise  # 重新抛出以结束生成器

    def chat(self, query, model):
        """非流式聊天响应"""
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
            with requests.post(self.api_url, headers=self.headers, json=payload, stream=True, verify=False) as response:
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
            converter = ThinkTagConverter()
            processed_text = converter.process(full_response) + converter.flush()
            result = {
                "id": "chatcmpl-" + str(uuid.uuid4()),
                "object": "chat.completion",
                "model": model,
                "choices": [{"message": {"role": "assistant", "content": processed_text}, "index": 0, "finish_reason": "stop"}]
            }
            return result
        except requests.exceptions.RequestException as e:
            return {"error": f"Request failed: {str(e)}"}

# 全局单例
client = ChatClient()

@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    req_data = request.get_json(force=True)
    model = req_data.get("model", "o3-mini")
    stream_flag = req_data.get("stream", False)
    messages = req_data.get("messages", [])

    if messages and messages[-1].get("content") == "clear":
        client.reset_conversation()
        return Response(client.reset_chat_response(), mimetype="text/event-stream")

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
