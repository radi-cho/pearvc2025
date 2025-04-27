export ANTHROPIC_API_KEY=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
export OPENAI_API_KEY=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

docker build . -t android-scrcpy:local

docker run -ti -d --privileged -v /dev/bus/usb:/dev/bus/usb \
    -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
    -v $HOME/.anthropic:/home/computeruse/.anthropic \
    -v ~/Projects/pearvc2025/anthropic-quickstarts/computer-use-demo/personal_info.txt:/home/computeruse/local_files/personal_info.txt \
    -p 5900:5900 \
    -p 8501:8501 \
    -p 6080:6080 \
    -p 8080:8080 \
    -it android-scrcpy:local