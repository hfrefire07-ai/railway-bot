from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp

from .config import Settings


class DownloadError(RuntimeError):
    """A user-facing error while retrieving an input script."""


def _validate_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise DownloadError("El enlace debe usar http:// o https:// y tener un dominio válido.")
    if parsed.username or parsed.password:
        raise DownloadError("Los enlaces con usuario o contraseña no están permitidos.")
    return url


async def _is_public_host(hostname: str) -> bool:
    try:
        addresses = await asyncio.to_thread(
            lambda: {item[4][0] for item in socket.getaddrinfo(hostname, None)}
        )
    except socket.gaierror as exc:
        raise DownloadError("No se pudo resolver el dominio del enlace.") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise DownloadError("Por seguridad, no se permiten enlaces hacia redes privadas.")
    return True


async def download_to_file(
    url: str,
    destination: Path,
    settings: Settings,
    *,
    suggested_name: str = "script.luau",
) -> str:
    """Stream a public URL to disk without loading the whole file in memory."""
    current_url = _validate_url(url)
    timeout = aiohttp.ClientTimeout(
        total=settings.raw_download_timeout_seconds,
        connect=20,
        sock_read=settings.raw_download_timeout_seconds,
    )
    headers = {"User-Agent": "LuauDeobfuscatorDiscordBot/1.0"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for redirect_number in range(settings.max_redirects + 1):
            parsed = urlparse(current_url)
            await _is_public_host(parsed.hostname or "")
            try:
                response = await session.get(
                    current_url,
                    allow_redirects=False,
                    read_until_eof=False,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise DownloadError("No se pudo descargar el enlace.") from exc

            async with response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location or redirect_number >= settings.max_redirects:
                        raise DownloadError("El enlace tiene demasiadas redirecciones.")
                    current_url = _validate_url(urljoin(current_url, location))
                    continue
                if response.status < 200 or response.status >= 300:
                    raise DownloadError(f"El enlace respondió con HTTP {response.status}.")

                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > settings.max_input_bytes:
                    raise DownloadError(
                        f"El archivo supera el máximo configurado de "
                        f"{settings.max_input_bytes // (1024 * 1024)} MiB."
                    )

                total = 0
                try:
                    with destination.open("wb") as output:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            total += len(chunk)
                            if total > settings.max_input_bytes:
                                raise DownloadError(
                                    f"El archivo supera el máximo configurado de "
                                    f"{settings.max_input_bytes // (1024 * 1024)} MiB."
                                )
                            output.write(chunk)
                except OSError as exc:
                    raise DownloadError("No se pudo guardar el archivo temporal.") from exc

                filename = Path(urlparse(current_url).path).name or suggested_name
                return filename[:120]

        raise DownloadError("No se pudo seguir el enlace.")