"""
Entrypoint for streamlit, see https://docs.streamlit.io/
"""

import asyncio
import base64
import os
import subprocess
import tempfile
import traceback
import time
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import PosixPath
from typing import cast, get_args, Optional
import base64

import httpx
import streamlit as st
from anthropic import RateLimitError
from anthropic.types.beta import (
    BetaContentBlockParam,
    BetaTextBlockParam,
    BetaToolResultBlockParam,
)
from streamlit.delta_generator import DeltaGenerator

from openai import OpenAI

from computer_use_demo.loop import (
    APIProvider,
    sampling_loop,
)
from computer_use_demo.tools import ToolResult, ToolVersion

PROVIDER_TO_DEFAULT_MODEL_NAME: dict[APIProvider, str] = {
    APIProvider.ANTHROPIC: "claude-3-7-sonnet-20250219",
    APIProvider.BEDROCK: "anthropic.claude-3-5-sonnet-20241022-v2:0",
    APIProvider.VERTEX: "claude-3-5-sonnet-v2@20241022",
}


@dataclass(kw_only=True, frozen=True)
class ModelConfig:
    tool_version: ToolVersion
    max_output_tokens: int
    default_output_tokens: int
    has_thinking: bool = False


SONNET_3_5_NEW = ModelConfig(
    tool_version="computer_use_20241022",
    max_output_tokens=1024 * 8,
    default_output_tokens=1024 * 4,
)

SONNET_3_7 = ModelConfig(
    tool_version="computer_use_20250124",
    max_output_tokens=128_000,
    default_output_tokens=1024 * 16,
    has_thinking=True,
)

MODEL_TO_MODEL_CONF: dict[str, ModelConfig] = {
    "claude-3-7-sonnet-20250219": SONNET_3_7,
}

CONFIG_DIR = PosixPath("~/.anthropic").expanduser()
API_KEY_FILE = CONFIG_DIR / "api_key"
OPENAI_API_KEY_FILE = CONFIG_DIR / "openai_api_key"

STREAMLIT_STYLE = """
<style>
    /* Highlight the stop button in red */
    button[kind=header] {
        background-color: rgb(255, 75, 75);
        border: 1px solid rgb(255, 75, 75);
        color: rgb(255, 255, 255);
    }
    button[kind=header]:hover {
        background-color: rgb(255, 51, 51);
    }
    /* Hide the streamlit deploy button */
    .stAppDeployButton {
        visibility: hidden;
    }
    
    /* Custom styling */
    h1 {
        color: #4a56e2 !important;
        text-decoration: underline;
    }
    
    .stButton > button {
        background-color: #4a56e2 !important;
        color: white !important;
    }
    
    /* Voice interface styling */
    .voice-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        padding: 20px;
        margin-bottom: 20px;
        background: rgba(74, 86, 226, 0.1);
        border-radius: 10px;
    }
    
    .record-button {
        width: 80px;
        height: 80px;
        border-radius: 50%;
        background-color: #4a56e2;
        color: white;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        font-size: 24px;
        margin: 20px 0;
        border: none;
    }
    
    .record-button:hover {
        background-color: #354ad6;
    }
    
    .record-button.recording {
        background-color: #ff4b4b;
        animation: pulse 1.5s infinite;
    }
    
    @keyframes pulse {
        0% {
            transform: scale(1);
        }
        50% {
            transform: scale(1.1);
        }
        100% {
            transform: scale(1);
        }
    }
    
    .waveform {
        width: 100%;
        height: 80px;
        background-color: #f8f9fa;
        border-radius: 5px;
        overflow: hidden;
        position: relative;
    }
    
    .transcript-area {
        margin-top: 20px;
        padding: 10px;
        border-radius: 5px;
        background-color: #f8f9fa;
        min-height: 50px;
    }
    
    /* Split interface into top and bottom */
    .voice-section {
        border-bottom: 1px solid #e0e0e0;
        padding-bottom: 20px;
        margin-bottom: 20px;
    }
    
    .response-section {
        max-height: 60vh;
        overflow-y: auto;
    }
</style>
"""

WARNING_TEXT = "⚠️ Security Alert: Never provide access to sensitive accounts or data, as malicious web content can hijack Claude's behavior"
INTERRUPT_TEXT = "(user stopped or interrupted and wrote the following)"
INTERRUPT_TOOL_ERROR = "human stopped or interrupted tool execution"


class Sender(StrEnum):
    USER = "user"
    BOT = "assistant"
    TOOL = "tool"

def autoplay_audio(file_path: str):
    with open(file_path, "rb") as f:
        data = f.read()
        b64 = base64.b64encode(data).decode()
        md = f"""
            <audio controls autoplay="true">
            <source src="data:audio/mp3;base64,{b64}" type="audio/mp3">
            </audio>
            """
        st.markdown(
            md,
            unsafe_allow_html=True,
        )

def setup_state():
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "api_key" not in st.session_state:
        # Try to load API key from file first, then environment
        st.session_state.api_key = load_from_storage("api_key") or os.getenv(
            "ANTHROPIC_API_KEY", ""
        )
    if "openai_api_key" not in st.session_state:
        st.session_state.openai_api_key = load_from_storage("openai_api_key") or os.getenv(
            "OPENAI_API_KEY", ""
        )
    if "provider" not in st.session_state:
        st.session_state.provider = (
            os.getenv("API_PROVIDER", "anthropic") or APIProvider.ANTHROPIC
        )
    if "provider_radio" not in st.session_state:
        st.session_state.provider_radio = st.session_state.provider
    if "model" not in st.session_state:
        _reset_model()
    if "auth_validated" not in st.session_state:
        st.session_state.auth_validated = False
    if "responses" not in st.session_state:
        st.session_state.responses = {}
    if "tools" not in st.session_state:
        st.session_state.tools = {}
    if "only_n_most_recent_images" not in st.session_state:
        st.session_state.only_n_most_recent_images = 3
    if "custom_system_prompt" not in st.session_state:
        st.session_state.custom_system_prompt = load_from_storage("system_prompt") or ""
    if "hide_images" not in st.session_state:
        st.session_state.hide_images = False
    if "token_efficient_tools_beta" not in st.session_state:
        st.session_state.token_efficient_tools_beta = False
    if "in_sampling_loop" not in st.session_state:
        st.session_state.in_sampling_loop = False
    if "transcript" not in st.session_state:
        st.session_state.transcript = ""


def _reset_model():
    st.session_state.model = PROVIDER_TO_DEFAULT_MODEL_NAME[
        cast(APIProvider, st.session_state.provider)
    ]
    _reset_model_conf()


def _reset_model_conf():
    model_conf = (
        SONNET_3_7
        if "3-7" in st.session_state.model
        else MODEL_TO_MODEL_CONF.get(st.session_state.model, SONNET_3_5_NEW)
    )

    # If we're in radio selection mode, use the selected tool version
    if hasattr(st.session_state, "tool_versions"):
        st.session_state.tool_version = st.session_state.tool_versions
    else:
        st.session_state.tool_version = model_conf.tool_version

    st.session_state.has_thinking = model_conf.has_thinking
    st.session_state.output_tokens = model_conf.default_output_tokens
    st.session_state.max_output_tokens = model_conf.max_output_tokens
    st.session_state.thinking_budget = int(model_conf.default_output_tokens / 2)


def record_and_transcribe():
    """Record audio and transcribe using OpenAI API"""
    if not st.session_state.openai_api_key:
        st.error("Please set your OpenAI API key in the sidebar.")
        return None
    
    # Record audio using Streamlit's native audio_input
    audio_bytes = st.audio_input("Record your voice message")
    
    if audio_bytes:
        with st.spinner("Transcribing audio..."):
            # Save the audio bytes to a temporary file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_audio:
                # Read the file data from the UploadedFile object
                temp_audio.write(audio_bytes.read())
                temp_audio_path = temp_audio.name
            
            # Transcribe the audio using OpenAI
            transcript = transcribe_audio(temp_audio_path)
            
            # Remove the temporary file
            try:
                os.unlink(temp_audio_path)
            except Exception as e:
                print(f"Error removing temporary file: {e}")
            
            if transcript:
                st.session_state.transcript = transcript
                return transcript
            else:
                st.error("Failed to transcribe audio.")
                return None
    
    return None


def transcribe_audio(audio_file_path):
    """Transcribe audio using OpenAI API"""
    try:
        client = OpenAI(api_key=st.session_state.openai_api_key)
        with open(audio_file_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe", 
                file=audio_file
            )
        return transcription.text
    except Exception as e:
        st.error(f"Error transcribing audio: {e}")
        return None


async def main():
    """Render loop for streamlit"""
    setup_state()

    st.markdown(STREAMLIT_STYLE, unsafe_allow_html=True)
    
    st.title("VIVA AI")
    
    # audio_path = "/home/computeruse/computer_use_demo/pacmac.mp3"

    # if os.path.exists(audio_path):
    #     # Display audio player
    #     st.audio(audio_path)
    #     st.write("Playing audio file: pacmac.mp3")
    # else:
    #     st.error(f"Error: Audio file not found at {audio_path}")

    with st.sidebar:
        def _reset_api_provider():
            if st.session_state.provider_radio != st.session_state.provider:
                _reset_model()
                st.session_state.provider = st.session_state.provider_radio
                st.session_state.auth_validated = False

        provider_options = [option.value for option in APIProvider]
        st.radio(
            "API Provider",
            options=provider_options,
            key="provider_radio",
            format_func=lambda x: x.title(),
            on_change=_reset_api_provider,
        )

        st.text_input("Model", key="model", on_change=_reset_model_conf)

        if st.session_state.provider == APIProvider.ANTHROPIC:
            st.text_input(
                "Anthropic API Key",
                type="password",
                key="api_key",
                on_change=lambda: save_to_storage("api_key", st.session_state.api_key),
            )
            
        st.text_input(
            "OpenAI API Key (for voice transcription)",
            type="password",
            key="openai_api_key",
            on_change=lambda: save_to_storage("openai_api_key", st.session_state.openai_api_key),
        )

        st.number_input(
            "Only send N most recent images",
            min_value=0,
            key="only_n_most_recent_images",
            help="To decrease the total tokens sent, remove older screenshots from the conversation",
        )
        st.text_area(
            "Custom System Prompt Suffix",
            key="custom_system_prompt",
            help="Additional instructions to append to the system prompt. see computer_use_demo/loop.py for the base system prompt.",
            on_change=lambda: save_to_storage(
                "system_prompt", st.session_state.custom_system_prompt
            ),
        )
        st.checkbox("Hide screenshots", key="hide_images")
        st.checkbox(
            "Enable token-efficient tools beta", key="token_efficient_tools_beta"
        )
        versions = get_args(ToolVersion)
        st.radio(
            "Tool Versions",
            key="tool_versions",
            options=versions,
            index=versions.index(st.session_state.tool_version),
            on_change=lambda: setattr(
                st.session_state, "tool_version", st.session_state.tool_versions
            ),
        )

        st.number_input("Max Output Tokens", key="output_tokens", step=1)

        st.checkbox("Thinking Enabled", key="thinking", value=False)
        st.number_input(
            "Thinking Budget",
            key="thinking_budget",
            max_value=st.session_state.max_output_tokens,
            step=1,
            disabled=not st.session_state.thinking,
        )

        if st.button("Reset", type="primary"):
            with st.spinner("Resetting..."):
                st.session_state.clear()
                setup_state()

                subprocess.run("pkill Xvfb; pkill tint2", shell=True)  # noqa: ASYNC221
                await asyncio.sleep(1)
                subprocess.run("./start_all.sh", shell=True)  # noqa: ASYNC221


    if not st.session_state.auth_validated:
        if auth_error := validate_auth(
            st.session_state.provider, st.session_state.api_key
        ):
            st.warning(f"Please resolve the following auth issue:\n\n{auth_error}")
            return
        else:
            st.session_state.auth_validated = True

    # Create tabs for main interface and logs
    main_tab, http_logs = st.tabs(["Voice Interface", "HTTP Exchange Logs"])

    with main_tab:
        # Split the interface into two sections
        voice_section = st.container()
        response_section = st.container()
        
        with voice_section:
            st.markdown('<div class="voice-section">', unsafe_allow_html=True)
            
            # Voice interface with improved layout
            # st.markdown('<div class="voice-container">', unsafe_allow_html=True)
            
            col1, col2, col3 = st.columns([1, 2, 1])
            with col2:
                # st.write("👇 Click the microphone below to speak to the agent")                
                audio_bytes = st.audio_input("Speak to the agent", key="audio_recorder")
                
                if audio_bytes:
                    with st.spinner("Transcribing..."):
                        # Save the audio bytes to a temporary file
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as temp_audio:
                            # Read the file data from the UploadedFile object
                            temp_audio.write(audio_bytes.read())
                            temp_audio_path = temp_audio.name
                        
                        # Transcribe the audio using OpenAI
                        transcript = transcribe_audio(temp_audio_path)
                        
                        # Remove the temporary file
                        try:
                            os.unlink(temp_audio_path)
                        except Exception as e:
                            print(f"Error removing temporary file: {e}")
                        
                        print("TRANSCRIBED AUDIO: ", transcript)
                        
                        if transcript:
                            st.session_state.transcript = transcript
                            # Add the transcript to messages
                            st.session_state.messages.append(
                                {
                                    "role": Sender.USER,
                                    "content": [
                                        *maybe_add_interruption_blocks(),
                                        BetaTextBlockParam(type="text", text=transcript),
                                    ],
                                }
                            )
                            # st.experimental_rerun()
                        else:
                            st.error("Failed to transcribe audio.")
            
            # Show transcript if available
            if st.session_state.transcript:
                st.markdown(f"<div class='transcript-area'><strong>You said:</strong> {st.session_state.transcript}</div>", unsafe_allow_html=True)
            
            # Option to type text instead
            with st.expander("Or type your message instead"):
                text_input = st.text_area("Type your message", key="text_input")
                if st.button("Send", key="send_text"):
                    if text_input.strip():
                        st.session_state.messages.append(
                            {
                                "role": Sender.USER,
                                "content": [
                                    *maybe_add_interruption_blocks(),
                                    BetaTextBlockParam(type="text", text=text_input),
                                ],
                            }
                        )
                        st.session_state.transcript = text_input
                        
                        st.session_state.messages.append(
                                {
                                    "role": Sender.USER,
                                    "content": [
                                        *maybe_add_interruption_blocks(),
                                        BetaTextBlockParam(type="text", text=text_input),
                                    ],
                                }
                            )
            
            st.markdown('</div>', unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
        
        with response_section:
            st.markdown('<div class="response-section">', unsafe_allow_html=True)
            st.subheader("Agent Response")
            
            # Show agent responses
            for message in st.session_state.messages:
                if message["role"] != Sender.USER:
                    if isinstance(message["content"], str):
                        _render_message(message["role"], message["content"])
                    elif isinstance(message["content"], list):
                        for block in message["content"]:
                            # the tool result we send back to the Anthropic API isn't sufficient to render all details,
                            # so we store the tool use responses
                            if isinstance(block, dict) and block["type"] == "tool_result":
                                _render_message(
                                    Sender.TOOL, st.session_state.tools[block["tool_use_id"]]
                                )
                            else:
                                _render_message(
                                    message["role"],
                                    cast(BetaContentBlockParam | ToolResult, block),
                                )
            
            st.markdown('</div>', unsafe_allow_html=True)

        # render past http exchanges
        for identity, (request, response) in st.session_state.responses.items():
            _render_api_response(request, response, identity, http_logs)

        # Run the agent when we have a new user message
        try:
            most_recent_message = st.session_state["messages"][-1]
        except IndexError:
            return

        if most_recent_message["role"] is Sender.USER:
            # Process the newest user message
            with track_sampling_loop():
                # run the agent sampling loop with the newest message
                st.session_state.messages = await sampling_loop(
                    system_prompt_suffix=st.session_state.custom_system_prompt,
                    model=st.session_state.model,
                    provider=st.session_state.provider,
                    messages=st.session_state.messages,
                    output_callback=partial(_render_message, Sender.BOT),
                    tool_output_callback=partial(
                        _tool_output_callback, tool_state=st.session_state.tools
                    ),
                    api_response_callback=partial(
                        _api_response_callback,
                        tab=http_logs,
                        response_state=st.session_state.responses,
                    ),
                    api_key=st.session_state.api_key,
                    only_n_most_recent_images=st.session_state.only_n_most_recent_images,
                    tool_version=st.session_state.tool_versions,
                    max_tokens=st.session_state.output_tokens,
                    thinking_budget=st.session_state.thinking_budget
                    if st.session_state.thinking
                    else None,
                    token_efficient_tools_beta=st.session_state.token_efficient_tools_beta,
                )
                # Clear the transcript after processing
                st.session_state.transcript = ""


def maybe_add_interruption_blocks():
    if not st.session_state.in_sampling_loop:
        return []
    # If this function is called while we're in the sampling loop, we can assume that the previous sampling loop was interrupted
    # and we should annotate the conversation with additional context for the model and heal any incomplete tool use calls
    result = []
    last_message = st.session_state.messages[-1]
    previous_tool_use_ids = [
        block["id"] for block in last_message["content"] if block["type"] == "tool_use"
    ]
    for tool_use_id in previous_tool_use_ids:
        st.session_state.tools[tool_use_id] = ToolResult(error=INTERRUPT_TOOL_ERROR)
        result.append(
            BetaToolResultBlockParam(
                tool_use_id=tool_use_id,
                type="tool_result",
                content=INTERRUPT_TOOL_ERROR,
                is_error=True,
            )
        )
    result.append(BetaTextBlockParam(type="text", text=INTERRUPT_TEXT))
    return result


@contextmanager
def track_sampling_loop():
    st.session_state.in_sampling_loop = True
    yield
    st.session_state.in_sampling_loop = False


def validate_auth(provider: APIProvider, api_key: str | None):
    if provider == APIProvider.ANTHROPIC:
        if not api_key:
            return "Enter your Anthropic API key in the sidebar to continue."
    if provider == APIProvider.BEDROCK:
        import boto3

        if not boto3.Session().get_credentials():
            return "You must have AWS credentials set up to use the Bedrock API."
    if provider == APIProvider.VERTEX:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        if not os.environ.get("CLOUD_ML_REGION"):
            return "Set the CLOUD_ML_REGION environment variable to use the Vertex API."
        try:
            google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        except DefaultCredentialsError:
            return "Your google cloud credentials are not set up correctly."


def load_from_storage(filename: str) -> str | None:
    """Load data from a file in the storage directory."""
    try:
        file_path = CONFIG_DIR / filename
        if file_path.exists():
            data = file_path.read_text().strip()
            if data:
                return data
    except Exception as e:
        st.write(f"Debug: Error loading {filename}: {e}")
    return None


def save_to_storage(filename: str, data: str) -> None:
    """Save data to a file in the storage directory."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = CONFIG_DIR / filename
        file_path.write_text(data)
        # Ensure only user can read/write the file
        file_path.chmod(0o600)
    except Exception as e:
        st.write(f"Debug: Error saving {filename}: {e}")


def _api_response_callback(
    request: httpx.Request,
    response: httpx.Response | object | None,
    error: Exception | None,
    tab: DeltaGenerator,
    response_state: dict[str, tuple[httpx.Request, httpx.Response | object | None]],
):
    """
    Handle an API response by storing it to state and rendering it.
    """
    response_id = datetime.now().isoformat()
    response_state[response_id] = (request, response)
    if error:
        _render_error(error)
    _render_api_response(request, response, response_id, tab)


def _tool_output_callback(
    tool_output: ToolResult, tool_id: str, tool_state: dict[str, ToolResult]
):
    """Handle a tool output by storing it to state and rendering it."""
    tool_state[tool_id] = tool_output
    _render_message(Sender.TOOL, tool_output)


def _render_api_response(
    request: httpx.Request,
    response: httpx.Response | object | None,
    response_id: str,
    tab: DeltaGenerator,
):
    """Render an API response to a streamlit tab"""
    with tab:
        with st.expander(f"Request/Response ({response_id})"):
            newline = "\n\n"
            st.markdown(
                f"`{request.method} {request.url}`{newline}{newline.join(f'`{k}: {v}`' for k, v in request.headers.items())}"
            )
            st.json(request.read().decode())
            st.markdown("---")
            if isinstance(response, httpx.Response):
                st.markdown(
                    f"`{response.status_code}`{newline}{newline.join(f'`{k}: {v}`' for k, v in response.headers.items())}"
                )
                st.json(response.text)
            else:
                st.write(response)


def _render_error(error: Exception):
    if isinstance(error, RateLimitError):
        body = "You have been rate limited."
        if retry_after := error.response.headers.get("retry-after"):
            body += f" **Retry after {str(timedelta(seconds=int(retry_after)))} (HH:MM:SS).** See our API [documentation](https://docs.anthropic.com/en/api/rate-limits) for more details."
        body += f"\n\n{error.message}"
    else:
        body = str(error)
        body += "\n\n**Traceback:**"
        lines = "\n".join(traceback.format_exception(error))
        body += f"\n\n```{lines}```"
    save_to_storage(f"error_{datetime.now().timestamp()}.md", body)
    st.error(f"**{error.__class__.__name__}**\n\n{body}", icon=":material/error:")


def say_text(text: str):
    client = OpenAI(api_key="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")
    speech_file_path = "/home/computeruse/speech.mp3"

    with client.audio.speech.with_streaming_response.create(
        model="gpt-4o-mini-tts",
        voice="coral",
        input=text,
        instructions="Speak very fast",
    ) as response:
        response.stream_to_file(speech_file_path)
    
    autoplay_audio(speech_file_path)

def _render_message(
    sender: Sender,
    message: str | BetaContentBlockParam | ToolResult,
):
    """Convert input from the user or output from the agent to a streamlit message."""
    # streamlit's hotreloading breaks isinstance checks, so we need to check for class names
    is_tool_result = not isinstance(message, str | dict)
    if not message or (
        is_tool_result
        and st.session_state.hide_images
        and not hasattr(message, "error")
        and not hasattr(message, "output")
    ):
        return
    with st.chat_message(sender):
        if is_tool_result:
            message = cast(ToolResult, message)
            if message.output:
                if message.__class__.__name__ == "CLIResult":
                    st.code(message.output)
                else:
                    st.markdown(message.output)
            if message.error:
                st.error(message.error)
            if message.base64_image and not st.session_state.hide_images:
                st.image(base64.b64decode(message.base64_image))
        elif isinstance(message, dict):
            if message["type"] == "text":
                st.write(message["text"])
                # say_text(message["text"])
            elif message["type"] == "thinking":
                thinking_content = message.get("thinking", "")
                st.markdown(f"[Thinking]\n\n{thinking_content}")
            elif message["type"] == "tool_use":
                st.code(f'Tool Use: {message["name"]}\nInput: {message["input"]}')
            else:
                # only expected return types are text and tool_use
                raise Exception(f'Unexpected response type {message["type"]}')
        else:
            st.markdown(message)


if __name__ == "__main__":
    asyncio.run(main())
