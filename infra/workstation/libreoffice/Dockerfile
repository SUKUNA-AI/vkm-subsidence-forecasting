# Pinned DOCX -> PDF renderer for the document pipeline (VKM-SRC-023, pages "r"; decision CP-08, H-14, H-50).
# Ubuntu 24.04 (noble) ships the LibreOffice 24.2.x branch, the branch Phase 1 used (24.2.7.2, headless).
# The image is built once and used by digest; the render profile (LibreOffice version, font packages and a hash of
# the font list) is written into the image and copied into every render receipt. Runs with --network none.
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC SAL_USE_VCLPLUGIN=svp

RUN apt-get update \
 && apt-get install -y libreoffice-writer libreoffice-draw fontconfig \
 && rm -rf /var/lib/apt/lists/*

RUN soffice --version | grep -q "LibreOffice 24\.2\." \
 && mkdir -p /opt/vkm \
 && soffice --version | head -1 > /opt/vkm/soffice_version.txt \
 && dpkg-query -W -f '${Package} ${Version}\n' | grep -E '^(libreoffice|fonts-|fontconfig)' | sort > /opt/vkm/packages.txt \
 && fc-list --format '%{file}|%{family}|%{style}\n' | sort > /opt/vkm/fonts.txt \
 && sha256sum /opt/vkm/fonts.txt | cut -c1-64 > /opt/vkm/fonts.sha256

# the base image already has the unprivileged user "ubuntu" (uid 1000)
USER ubuntu
WORKDIR /work
ENTRYPOINT ["soffice", "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nofirststartwizard"]
