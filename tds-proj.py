from dotenv import load_dotenv
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
import os
import subprocess
import json
import tempfile
import re
import requests

# Load environment variables
load_dotenv()

# Initialize FastAPI app
app = FastAPI()

# Load API token
AIPROXY_TOKEN = os.getenv("AIPROXY_TOKEN")
if not AIPROXY_TOKEN:
    raise ValueError("AIPROXY_TOKEN is not set. Please check your .env file.")

# AI Proxy base URL
AIPROXY_BASE_URL = "https://aiproxy.sanand.workers.dev/openai"

def is_safe_command(command: str) -> bool:
    """Ensures the command does not access data outside C:\\data and does not delete files."""
    
    # Prevent delete operations
    restricted_patterns = [
        r'\b(rm|del|erase|shutil\.rmtree|os\.remove|os\.rmdir|os\.removedirs|Path\.unlink)\b'
    ]

    # Check for restricted commands
    for pattern in restricted_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            return False

    return True

def convert_path_to_windows(path):
    if path.startswith('/'):
        path = path.replace('/', '\\')
    return path

def execute_task(command: str, execution_type: str):
    try:
        if not is_safe_command(command):
            raise HTTPException(status_code=403, detail="Access Denied")
    
        if execution_type == "shell":
            commands = command.split(';')  # Split commands by ';'
            script_content = '\n'.join(cmd.strip() for cmd in commands)  # Join commands into a script
            with tempfile.NamedTemporaryFile(delete=False, suffix='.bat', mode='w', encoding='utf-8') as temp_file:
                temp_file.write(script_content)  # Write the script to a file
                temp_script_path = temp_file.name
                
            # Execute the script using cmd.exe
            result = subprocess.run(['cmd.exe', '/c', temp_script_path], capture_output=True, text=True)
            os.remove(temp_script_path)
            return result.stdout  # Return the output of the script execution
        
        elif execution_type == "python":
            if not command.strip():
                return {"status": "error", "error": "Empty command received"}
            # Create a temporary file for script execution
            with tempfile.NamedTemporaryFile(delete=False, suffix=".py", mode="w", encoding="utf-8") as temp_file:
                temp_file.write(command)
                temp_script_path = temp_file.name  

            # Ensure script dependencies are installed
            install_missing_dependencies(command)

            # Execute the Python script
            result = subprocess.run(["python", temp_script_path], capture_output=True, text=True)
            
            # Cleanup: Remove temp script after execution
            os.remove(temp_script_path)
            
            if result.returncode == 0:
                return {"status": "success", "output": result.stdout.strip()}
            else:
                return {"status": "error", "error": result.stderr.strip()}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def install_missing_dependencies(python_code: str):
    """Checks for missing Python modules and installs them."""
    import re
    import subprocess

    # List of essential libraries to check
    essential_libraries = ['dateutil', 'numpy'] 

    # Find all imported modules in the provided Python code
    matches = re.findall(r'^\s*(?:import|from)\s+([a-zA-Z0-9_]+)', python_code, re.MULTILINE)
    if not matches:
        return

    # Combine found modules with essential libraries
    missing_modules = set(matches).union(essential_libraries)

    for module in missing_modules:
        try:
            __import__(module)
        except ImportError:
            print(f"Installing missing module: {module}")
            subprocess.run(["pip", "install", module], capture_output=True, text=True)

@app.post("/run")
async def run_task(task: str):
    components = task.split()
    
    for component in components:
        # Check if the component looks like a path
        if component.startswith('/') and ('/data/' not in component):
            raise HTTPException(status_code=403, detail="Access Denied")

    prompt = r'''
    
You are a DataWorks automation agent that generates fully executable Python or Windows shell scripts. 
Your response must always be valid JSON and contain syntactically correct, runnable code.
Make sure the Python scripts are fully executable and the shell commands are properly formatted.
The python script should be a ***SINGLE SCRIPT with ACTUAL NEW LINES*** and ***SHOULD NOT CONTAIN '\n'***.
For shell if it's multiple commands, separate them by ;
**TASKS**
    - Data processing, API fetching (save under C:\data), Git (clone/commit under C:\data), web scraping, image/audio processing, format conversions, CSV/JSON filtering, external script execution.
    - Should generate only PYTHON or Windows CMD commands.
**Rules**
- Executable Code Only: The output must be fully executable Python code or shell commands.
- Valid Syntax: Ensure scripts are syntactically correct with no syntax errors (e.g., unterminated strings, missing brackets).
- [IMPORTANT] Newline Handling:
    **- Python scripts must USE ACTUAL NEWLINES, not \n inside strings.**
    - For multi-line strings, use triple quotes (""").
    - DO NOT generate Python code as a single string containing \n characters.
    - Every line of code should be written as a separate line, just like in a normal script.
    
- String Formatting:
    Always use double quotes (") for file paths and strings in Python.
    Use raw strings (r"") for file paths.
    **DO NOT USE '\n' inside strings.**
- File Operations: Perform all operations on files inside C:\data\.
- Libraries & Dependencies
    Use appropriate libraries:
        - pillow for image processing
        - sqlite3 for database operations
        - Dates: Handle and standardize dates from any format (e.g., YYYY-MM-DD, DD-MMM-YYYY, MMM DD, YYYY, YYYY/MM/DD HH:MM:SS, etc.) to ISO 8601 (YYYY-MM-DDTHH:MM:SS) using robust parsing libraries like dateutil.parser with error handling for invalid formats. If question is about counting / anything related to the dates'
    Ensure error handling for invalid data formats, especially for dates.
    
**Handling Images**
    - Convert images to Base64 encoding.    
    - Do not overwrite existing images.
    - Include **"type": "image_url"** and the encoded image in the JSON response.
    - If image processing or text extraction via LLM is required, use:
        For Images API: "https://aiproxy.sanand.workers.dev/openai/v1/chat/completions". Keep verification as false in response(verify=False)
        For Text API: "https://aiproxy.sanand.workers.dev/openai/v1/chat/completions" and model as "gpt-4o-mini". Keep verification as false in response(verify=False)
        Auth: Bearer {os.environ['AIPROXY_TOKEN']}
        Handle API errors with appropriate error messages.
    write code to pass the image to the API and retrieve the relevant text.

**Output Format**
    Return only valid JSON in the following format (no markdown or code blocks):
    json
    {
        "command": "<Fully Executable Python script or Shell command with proper syntax>",
        "type": "<python or shell>"
    }
'''
    try:
        # Use AI Proxy instead of direct OpenAI API
        response = requests.post(
            f"{AIPROXY_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {AIPROXY_TOKEN}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": task}
                ]
            },
            verify=False
        )
        
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"LLM API error: {response.text}")
            
        rjson = response.json()
        response_text = rjson["choices"][0]["message"]["content"].strip()

        if response_text.startswith("```json"):
            response_text = response_text[7:].strip()
        if response_text.endswith("```"):
            response_text = response_text[:-3].strip()

        
        # Debugging: Print the generated command
        print("Generated Command:", response_text)

        step = json.loads(response_text)
        if not isinstance(step, dict) or "command" not in step or "type" not in step:
            raise HTTPException(status_code=400, detail="Invalid task format received from LLM")

        step["command"] = convert_path_to_windows(step["command"])
        # Check for paths in the generated code
        components = step["command"].split()  # Split the generated code into components


        # Ensure the command is properly formatted
        if not isinstance(step, dict) or "command" not in step or "type" not in step:
            raise HTTPException(status_code=400, detail="Invalid task format received from LLM")

        # Convert paths and check for safety
        step["command"] = convert_path_to_windows(step["command"])
        for component in components:
            # Check if the component looks like a path
            if component.startswith('C:\\') and 'C:\\data\\' not in component:
                raise HTTPException(status_code=403, detail="Access Denied")
        print('Generated Command:', step["command"])

        result = execute_task(step["command"], step["type"])
        return {"status": "completed", "results": [result]}

    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="LLM returned an invalid JSON response")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/read")
async def read_file(path: str = Query(..., description="File path to read")):
    """Returns the content of the specified file, only if it's inside C:\\data."""
    try:
        if not re.match(r'^/data/.*', path):
            raise HTTPException(status_code=403, detail="Access Denied")
        
        absolute_path = os.path.join("C:/", path.lstrip('/'))  # Remove leading '/' and join
        
        if not os.path.exists(absolute_path):
            raise HTTPException(status_code=404, detail="File not found.")

        with open(absolute_path, "r", encoding="utf-8") as file:
            content = file.read()

        return JSONResponse(content={"content": content})

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/", response_class=PlainTextResponse)
def display_homepage():
    return "Welcome to Localhost"