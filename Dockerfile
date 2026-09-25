# Image dédiée pour les pods de transformation docx -> markdown.
# Construite une fois, poussée sur un registre accessible par le cluster,
# puis réutilisée par tous les pods Argo : plus besoin d'installer
# LibreOffice à chaque démarrage (lent, et source d'échecs silencieux selon
# les droits/le réseau du pod).

FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libreoffice \
        git \
    && rm -rf /var/lib/apt/lists/*

# uv pour installer les dépendances du projet (docling, markitdown, s3fs, ...)
RUN pip install --no-cache-dir uv

# Vérification au moment du build : le build échoue immédiatement si
# LibreOffice n'est finalement pas accessible, plutôt que de le découvrir
# en production.
RUN which soffice || which libreoffice

WORKDIR /app

CMD ["/bin/bash"]