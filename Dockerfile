# tidy-calibre-library in a container: Python, calibre's own Linux build for ebook-convert,
# poppler and djvulibre. To change the calibre version, see CONTRIBUTING.md.
FROM python:3.14.10-slim-trixie AS calibre
ARG CALIBRE=9.17.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
 && case "$(dpkg --print-architecture)" in \
      arm64) arch=arm64  sha=4cf033e6b4cd511c7b8db0d1d8e63f245a07416dbc3a96ebb95ec88d78aebeb6d2b91638ef56650eeb1129bd87ef49132a8d36ea179896dfc24b0680e0bcd5fb ;; \
      amd64) arch=x86_64 sha=9faf38f6bb0593598208fbde1be247d698a98d9f16e130ca6507049a02b1d9b65f7d10eeca15f5073d0372f7bb2d45d6c63d39a37f1b13a3cbaf81918e715711 ;; \
    esac \
 && curl -fsSL -o /tmp/calibre.txz "https://download.calibre-ebook.com/$CALIBRE/calibre-$CALIBRE-$arch.txz" \
 && echo "$sha  /tmp/calibre.txz" | sha512sum -c - \
 && mkdir /opt/calibre && tar xJf /tmp/calibre.txz -C /opt/calibre
# Converting to EPUB needs none of these: the web engine (PDF output, the viewer), text to speech
# and audio. Remove whole library names only: lib/libav* also takes libavif, and then 5 formats fail.
RUN cd /opt/calibre \
 && rm -rf lib/libQt6WebEngine* lib/calibre-extensions/PyQt6.QtWebEngine* libexec translations \
           resources/qtwebengine_* resources/icudtl.dat \
           lib/libonnxruntime* lib/libavcodec* lib/libavdevice* lib/libavfilter* lib/libavformat* lib/libavutil* \
           lib/libswresample* lib/libswscale* lib/libespeak* share/espeak-ng-data

FROM python:3.14.10-slim-trixie
# Qt loads libEGL, libGLX, libOpenGL and libxkbcommon, or every conversion fails (ebook-convert --version
# still works). libegl1 also brings Mesa and LLVM for a GPU, which offscreen Qt does not use.
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils djvulibre-bin libegl1 libglx0 libopengl0 libxkbcommon0 \
 && rm -rf /var/lib/apt/lists/* /usr/lib/*/libLLVM* /usr/lib/*/libgallium* /usr/lib/*/libz3* /usr/lib/*/dri
COPY --from=calibre /opt/calibre /opt/calibre
COPY requirements.txt /app/
COPY tidy_calibre_library/__init__.py tidy_calibre_library/words.py /app/tidy_calibre_library/
# The checks use wordfreq's English lists only.
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && SP=$(python -c "import site; print(site.getsitepackages()[0])") \
 && find "$SP/wordfreq/data" -type f ! -name '*_en.*' -delete \
 && find "$SP/numpy" -type d -name tests -prune -exec rm -rf {} + \
 && cd /app && python -c "from tidy_calibre_library import words; words.fetch('/app/words.txt')"
COPY tidy_calibre_library/ /app/tidy_calibre_library/
COPY tests.py fixture.txt /app/
ENV PATH=/opt/calibre:$PATH HOME=/tmp CALIBRE_CONFIG_DIRECTORY=/tmp/calibre QT_QPA_PLATFORM=offscreen PYTHONUNBUFFERED=1 XDG_CACHE_HOME=/cache PYTHONPATH=/app QA_WORDS=/app/words.txt
WORKDIR /work
ENTRYPOINT ["python", "-m", "tidy_calibre_library.validate"]
