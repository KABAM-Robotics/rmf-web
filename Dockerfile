    #-----------------------
    # Stage 1 - Dependencies
    #-----------------------
    ARG ROS_DISTRO=jazzy
    FROM ros:$ROS_DISTRO-ros-core
    ENV DEBIAN_FRONTEND=noninteractive

    RUN apt update && apt install -y \
    git \
    curl \
    build-essential \
    ros-$ROS_DISTRO-rmf-building-map-msgs \
    ros-$ROS_DISTRO-rmw-cyclonedds-cpp \
    python3-rosdep \
    python3-venv \
    python3-colcon-common-extensions \
    python3-pip
    
    WORKDIR /rmf_ws/src
    RUN git clone https://github.com/open-rmf/rmf_internal_msgs.git
    # Add Alert, AlertParameter, and AlertResponse msgs
    WORKDIR /rmf_ws/src/rmf_internal_msgs
    RUN git checkout 9ff25f4

    # colcon compilation
    WORKDIR /rmf_ws/
    RUN rosdep init && rosdep update --rosdistro jazzy \
        && rosdep install --from-paths src --ignore-src --rosdistro jazzy -yr
    RUN . /opt/ros/jazzy/setup.sh \
        && colcon build --cmake-args -DCMAKE_BUILD_TYPE=Release
    
    # cleanup
    RUN sed -i '$isource "/rmf_ws/install/setup.bash"' /ros_entrypoint.sh

    RUN curl -fsSL https://get.pnpm.io/install.sh | bash -
    ENV PNPM_HOME=/root/.local/share/pnpm
    ENV PATH="$PNPM_HOME:$PATH"
    RUN pnpm env use --global lts
    RUN curl -sL https://deb.nodesource.com/setup_16.x | bash -
    RUN apt-get update && apt-get install -y \
        nodejs \
        jq \
      && rm -rf /var/lib/apt/lists/*
    RUN npm install -g npm@11.1.0

    # Create a virtual environment and install packages in it
    WORKDIR /rmf-web/
    RUN python3 -m venv .venv \
        && ./.venv/bin/pip install pipenv uvicorn fastapi==0.109.0 termcolor tortoise-orm schedule python-socketio reactivex pyjwt pyyaml numpy websockets pydantic==2.8.0 toml==0.10.2

    WORKDIR /
    COPY ./ ./rmf-web

    #-----------------
    # Stage 2 - build
    #-----------------
    
    WORKDIR /rmf-web/
    RUN pnpm config set unsafe-perm
    RUN pnpm install
    RUN pnpm store prune
    WORKDIR /rmf-web/packages/rmf-dashboard-framework
    
    #----------
    # Stage 3 - cleanup
    #----------
    
    ENTRYPOINT ["/ros_entrypoint.sh"]
    CMD ["pnpm", "start"]
    
