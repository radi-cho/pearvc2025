# Vivus AI - Anthropic x Pear VC hackathon

This project leverages a computer agent built on top of **Anthropic's stack**, including **Claude**, the **Anthropic API**, and other key technologies. We've extended it with several key features:

- **Tool Use**: The agent can interact with external tools for enhanced functionality.
- **Multi-Agent Planning**: It can perform complex planning tasks by leveraging the capabilities of multiple agents.
- **3-way System Integration**: Importantly, the AI agent can interface with and control physical devices, bridging the gap between virtual and physical worlds. It can access browsers, mobile phones, and local information in operating systems, to create a combined interface that maximizes personalization.

## Development Environment Setup

To run the application, you need to prepare the development environment by running the following networking commands:

```bash
adb kill-server
adb -a nodaemon server start
```

For using the application, run
```bash
docker build -t vivus .

docker run -ti -d --privileged -v /dev/bus/usb:/dev/bus/usb \
    -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
    -v $HOME/.anthropic:/home/computeruse/.anthropic \
    -p 5900:5900 \
    -p 8501:8501 \
    -p 6080:6080 \
    -p 8080:8080 \
    vivus
```
