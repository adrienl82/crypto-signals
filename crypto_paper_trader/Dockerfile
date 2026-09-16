ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app

# llama-cpp-python n'a pas de wheel precompilee pour musl/Alpine (encore moins
# pour aarch64) : elle est compilee depuis les sources au build de l'image,
# d'ou le besoin de cmake/g++/make. CMAKE_ARGS desactive les backends GPU
# (inexistants sur un Pi) pour ne compiler que le chemin CPU.
RUN apk add --no-cache cmake g++ make git

COPY requirements.txt .
RUN CMAKE_ARGS="-DGGML_CUDA=off -DGGML_METAL=off" pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD [ "python3", "/app/run_loop.py" ]
