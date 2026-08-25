"""Real-SDK guards for the 2026-08-24 azure-mgmt-storage incident.

If you add a new call into this SDK, extend this file.
"""

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.test import SimpleTestCase


class CryptographySdkContractTest(SimpleTestCase):
    def test_aesgcm_encrypt_decrypt_round_trip_and_invalid_tag(self):
        aes = AESGCM(b"k" * 32)
        nonce = b"n" * 12
        ciphertext = aes.encrypt(nonce, b"plaintext", b"tenant:table:column")

        self.assertEqual(aes.decrypt(nonce, ciphertext, b"tenant:table:column"), b"plaintext")
        with self.assertRaises(InvalidTag):
            aes.decrypt(nonce, ciphertext, b"wrong-aad")

    def test_hkdf_derives_the_length_we_rely_on(self):
        derived = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"",
            info=b"content-v1",
        ).derive(b"d" * 32)

        self.assertEqual(len(derived), 32)

    def test_fernet_multifernet_encrypt_decrypt_and_rotate(self):
        old = Fernet(Fernet.generate_key())
        new = Fernet(Fernet.generate_key())
        old_ring = MultiFernet([old])
        new_ring = MultiFernet([new, old])
        token = old_ring.encrypt(b"refresh-token")
        rotated = new_ring.rotate(token)

        self.assertEqual(new_ring.decrypt(rotated), b"refresh-token")
        self.assertEqual(new.decrypt(rotated), b"refresh-token")
        with self.assertRaises(InvalidToken):
            Fernet(Fernet.generate_key()).decrypt(rotated)

    def test_p256_pem_loading_shape_and_exceptions_exist(self):
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        loaded = serialization.load_pem_private_key(pem, password=None)

        self.assertIsInstance(loaded, ec.EllipticCurvePrivateKey)
        self.assertIsInstance(loaded.curve, ec.SECP256R1)
        self.assertTrue(issubclass(UnsupportedAlgorithm, Exception))
