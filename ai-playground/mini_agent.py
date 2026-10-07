import json

MAX_STEPS = 10

def system_prompt():
    return {"role": "system", "content": "You are a helpful assistant."}


def call_llm(messages, tools):
    import os
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ.get('DEEPSEEK_API_KEY'),
        base_url="https://api.deepseek.com"
    )

    response = client.chat.completions.create(
        model="deepseek-flash",
        messages=messages,
        stream=False,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
        tools=tools
    )
    return response.choices[0].message

def read_file(file_path: str) -> str:
    with open(file_path, "r") as f:
        return f.read()

def write_file(file_path: str, content: str) -> None:
    with open(file_path, "w") as f:
        f.write(content)

def list_files(directory: str) -> list[str]:
    import os
    return os.listdir(directory)

def run_shell(command: str) -> str:
    import subprocess
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout + result.stderr


def tool_message(tool_call_id: str, content: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": content
    }

tools = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the content of a file, the user should supply a file path first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The path to the file to read",
                    }
                },
                "required": ["file_path"]
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, the user should supply a file path and content first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The path to the file to write",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write to the file",
                    }
                },
                "required": ["file_path", "content"]
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in a directory, the user should supply a directory path first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "The path to the directory to list files from",
                    }
                },
                "required": ["directory"]
            },
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Execute a shell command, the user should supply the command first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    }
                },
                "required": ["command"]
            },
        }
    }
]

tool_registry = {
    'read_file': read_file,
    'write_file': write_file,
    'list_files': list_files,
    'run_shell': run_shell,
}

def execute_tool(call) -> str:
    function_name = call.name
    args = call.arguments
    result = tool_registry[function_name](**json.loads(args))
    return json.dumps(result)

def run_agent(task: str) -> str:
    messages = [system_prompt(), {"role": "user", "content": task}]

    for _ in range(MAX_STEPS):
        response = call_llm(messages, tools=tools)

        if not response.tool_calls:
            return response.content                      # done

        messages.append(
            {
                "role": "assistant",
                "content": response.content,
                "tool_calls": response.tool_calls,
                "tool_call_id": response.tool_calls[0].id if response.tool_calls else None,
                "function": response.tool_calls[0].function.name if response.tool_calls else None,
                "tool_arguments": response.tool_calls[0].function.arguments if response.tool_calls else None,
            }
        )


        for call in response.tool_calls:
            try:
                result = execute_tool(call.function)
            except Exception as e:
                result = f"Tool failed: {type(e).__name__}: {e}"
            messages.append(tool_message(call.id, result))  # matching tool_call_id

    return "Stopped: max steps reached"


def main():
    task = input("Enter the task for the agent: ")
    result = run_agent(task)
    print(result)

if __name__ == "__main__":
    main()