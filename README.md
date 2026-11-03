# Historias GovTech

Flujo para publicar historias de Instagram casi en tiempo real durante el GovTech Summit 2026.

1. Desde el celular, en `web/` (publicada en Vercel), se suben fotos o videos con una descripción, la zona y el formato.
2. Los archivos llegan directo al PC por un túnel de Cloudflare (`pc/server.py`), aunque el celular y el PC estén en redes distintas.
3. Claude ve el material, escribe el texto y arma la historia con `pc/render.py` (1080x1920, H.264).
4. La historia vuelve al celular con vista previa, para descargarla o compartirla a Instagram. Si algo no cuadra, se pide la corrección desde ahí mismo.

## Formatos

Video o imagen con texto, tres videos apilados, imagen/video/imagen, cortes rápidos y la nota animada de bienvenida.

## Encender el PC

```
cd pc
python -m venv .venv
.venv\Scripts\pip install pillow pillow-heif imageio-ffmpeg numpy
.venv\Scripts\python start.py
```

`start.py` levanta el servidor, abre el túnel y publica su dirección en `web/tunnel.json`. El PIN queda en `pc/data/secret.json`, que no se sube al repositorio, igual que el material del evento.

Fuentes: Raleway, Inter y Libre Baskerville (SIL Open Font License).
