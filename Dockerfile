# VFB image derivatives: thumbnails, WLZ and point-cloud OBJ for the image store (see README).
FROM python:3.12-slim-bookworm

ARG WOOLZ_REF=master
ARG NRRDTOOLS_REF=4e7594d

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential autoconf automake libtool pkg-config git ca-certificates \
      libjpeg-dev libtiff-dev libnifti2-dev libcjson-dev libbz2-dev liblzma-dev zlib1g-dev \
      imagemagick \
    && rm -rf /var/lib/apt/lists/*

# Woolz (WlzExtFFConvert reads gzip NRRD directly, so no Fiji/TIFF step). No -march flags: the cluster's
# VMs expose a qemu64 CPU without SSE4.2/POPCNT, so nothing here may assume x86-64-v2.
RUN git clone https://github.com/ma-tech/Woolz.git /tmp/woolz && cd /tmp/woolz && git checkout ${WOOLZ_REF} \
    && autoreconf -i --force \
    && CPPFLAGS="-I/usr/include/nifti -I/usr/include/cjson" ./configure --prefix=/opt/MouseAtlas \
         --enable-optimise --enable-openmp --enable-extff --with-pic --enable-static --disable-shared \
    && make -j"$(nproc)" && make install && cd / && rm -rf /tmp/woolz \
    && /opt/MouseAtlas/bin/WlzFacts -h 2>&1 | head -1

# numpy < 2.4: 2.4+ wheels need x86-64-v2 (the hosts are qemu64).
RUN pip install --no-cache-dir "numpy<2.4" pynrrd "scipy<1.15" pillow matplotlib pytest

RUN git clone https://github.com/Robbie1977/NRRDtools.git /opt/NRRDtools && cd /opt/NRRDtools && git checkout ${NRRDTOOLS_REF}

WORKDIR /app
COPY derive /app/derive
COPY tests /app/tests
RUN python -m pytest -q tests
ENV PYTHONPATH=/app NRRDTOOLS=/opt/NRRDtools WOOLZ_BIN=/opt/MouseAtlas/bin PYTHONUNBUFFERED=1
USER 1000:1000
