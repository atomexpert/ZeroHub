from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import CERTS_DIR, get_network_endpoints

logger = logging.getLogger("agy_hub.certs")

CA_CERT_PATH = CERTS_DIR / "ca_cert.pem"
CA_KEY_PATH = CERTS_DIR / "ca_key.pem"
SERVER_CERT_PATH = CERTS_DIR / "server_cert.pem"
SERVER_KEY_PATH = CERTS_DIR / "server_key.pem"


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _generate_ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _save_key_and_cert(
    key: ec.EllipticCurvePrivateKey,
    cert: x509.Certificate,
    key_path: Path,
    cert_path: Path,
) -> None:
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _create_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = _generate_ec_key()
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Antigravity Hub"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Antigravity Hub Local CA"),
        ]
    )
    now = _utc_now()
    pub = key.public_key()
    ski = x509.SubjectKeyIdentifier.from_public_key(pub)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ski, critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(pub), critical=False)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    _save_key_and_cert(key, cert, CA_KEY_PATH, CA_CERT_PATH)
    logger.info("Generated local Root CA at %s", CA_CERT_PATH)
    return key, cert


def _load_or_create_ca() -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    if CA_KEY_PATH.exists() and CA_CERT_PATH.exists():
        try:
            key = serialization.load_pem_private_key(
                CA_KEY_PATH.read_bytes(), password=None
            )
            cert = x509.load_pem_x509_certificate(CA_CERT_PATH.read_bytes())
            if cert.not_valid_after_utc > _utc_now() + datetime.timedelta(days=30):
                return key, cert
        except Exception as exc:
            logger.warning("Failed to load existing CA, recreating: %s", exc)
    return _create_ca()


def _collect_san_entries() -> tuple[list[str], list[ipaddress.IPv4Address]]:
    dns_names: list[str] = ["localhost"]
    try:
        hn = socket.gethostname().strip()
        if hn and hn.lower() != "localhost":
            dns_names.append(hn)
    except Exception:
        pass

    net = get_network_endpoints()
    ip_strs = ["127.0.0.1", *net.get("lan_ips", []), *net.get("tailscale_ips", [])]
    ips: list[ipaddress.IPv4Address] = []
    seen_ip: set[str] = set()
    for s in ip_strs:
        if s not in seen_ip:
            try:
                ips.append(ipaddress.ip_address(s))
                seen_ip.add(s)
            except ValueError:
                pass
    return dns_names, ips


def _server_cert_is_valid_for_current_host(
    ca_cert: x509.Certificate,
    expected_dns: list[str],
    expected_ips: list[ipaddress.IPv4Address],
) -> bool:
    if not (SERVER_CERT_PATH.exists() and SERVER_KEY_PATH.exists()):
        return False
    try:
        _ = serialization.load_pem_private_key(
            SERVER_KEY_PATH.read_bytes(), password=None
        )
        srv = x509.load_pem_x509_certificate(SERVER_CERT_PATH.read_bytes())
        if srv.not_valid_after_utc < _utc_now() + datetime.timedelta(days=30):
            return False
        if srv.issuer != ca_cert.subject:
            return False
        san = srv.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        cert_dns = set(san.get_values_for_type(x509.DNSName))
        cert_ips = set(san.get_values_for_type(x509.IPAddress))
        if not set(expected_dns).issubset(cert_dns):
            return False
        if not set(expected_ips).issubset(cert_ips):
            return False
        return True
    except Exception:
        return False


def ensure_https_certificates() -> tuple[Path, Path]:
    """Ensure Root CA and dynamic LAN/Tailscale SAN server cert exist on D:\\."""
    ca_key, ca_cert = _load_or_create_ca()
    dns_names, ips = _collect_san_entries()

    if _server_cert_is_valid_for_current_host(ca_cert, dns_names, ips):
        return SERVER_CERT_PATH, SERVER_KEY_PATH

    srv_key = _generate_ec_key()
    srv_pub = srv_key.public_key()
    now = _utc_now()

    san_list: list[x509.GeneralName] = [x509.DNSName(d) for d in dns_names] + [
        x509.IPAddress(ip) for ip in ips
    ]
    ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value

    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Antigravity Hub"),
                    x509.NameAttribute(NameOID.COMMON_NAME, "Antigravity Hub Local Server"),
                ]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(srv_pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=800))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(srv_pub), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
            critical=False,
        )
        .sign(private_key=ca_key, algorithm=hashes.SHA256())
    )

    _save_key_and_cert(srv_key, srv_cert, SERVER_KEY_PATH, SERVER_CERT_PATH)
    logger.info(
        "Generated HTTPS server cert for IPs=%s DNS=%s",
        [str(x) for x in ips],
        dns_names,
    )
    return SERVER_CERT_PATH, SERVER_KEY_PATH


def get_ca_cert_bytes() -> bytes:
    ensure_https_certificates()
    return CA_CERT_PATH.read_bytes()
