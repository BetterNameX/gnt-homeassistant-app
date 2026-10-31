"""Tests for custom_components/zendo/device_auth.py, against the shared vectors.

The vectors file is shared with the Zendo app and the Homey app, which check the
same cases. Run from the repository root:

    python3 -m unittest discover -s tests -v

Needs PyJWT and cryptography, which Home Assistant ships with, and nothing else
from Home Assistant: the module is loaded from its file, not as part of the
integration's package.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "custom_components" / "zendo" / "device_auth.py"
VECTORS = json.loads(
    (Path(__file__).resolve().parent / "companion-app-authentication-vectors.json").read_text()
)

_spec = importlib.util.spec_from_file_location("bngnt_device_auth", MODULE_PATH)
device_auth = importlib.util.module_from_spec(_spec)
sys.modules["bngnt_device_auth"] = device_auth
_spec.loader.exec_module(device_auth)

NOW = VECTORS["now"]
SITE_ID = VECTORS["linkedSiteId"]


def _keys():
    return device_auth.parse_keys_record(VECTORS["keysRecord"]["content"]).keys


class KeysRecordTest(unittest.TestCase):
    def test_cloudflare_answer_joins_its_strings(self):
        answer = VECTORS["keysRecord"]["dohJsonAnswers"]["cloudflare"]["Answer"][0]
        self.assertEqual(
            device_auth.parse_txt_data(answer["data"]), VECTORS["keysRecord"]["content"]
        )

    def test_google_answer_is_already_joined(self):
        answer = VECTORS["keysRecord"]["dohJsonAnswers"]["google"]["Answer"][0]
        self.assertEqual(
            device_auth.parse_txt_data(answer["data"]), VECTORS["keysRecord"]["content"]
        )

    def test_txt_strings_join_to_the_content(self):
        self.assertEqual(
            "".join(VECTORS["keysRecord"]["txtStrings"]), VECTORS["keysRecord"]["content"]
        )

    def test_broken_quoting_is_an_error(self):
        with self.assertRaises(ValueError):
            device_auth.parse_txt_data('"unterminated')
        with self.assertRaises(ValueError):
            device_auth.parse_txt_data('"one" two')

    def test_decimal_escapes(self):
        self.assertEqual(device_auth.parse_txt_data('"a\\034b"'), 'a"b')

    def test_record_parses_both_keys(self):
        record = device_auth.parse_keys_record(VECTORS["keysRecord"]["content"])
        self.assertEqual(
            {key.kid for key in record.keys},
            {VECTORS["keys"]["current"]["kid"], VECTORS["keys"]["next"]["kid"]},
        )
        self.assertEqual(record.ttl, VECTORS["keysRecord"]["parsed"]["ttl"])
        self.assertEqual(record.skipped, 0)

    def test_kid_follows_the_rule(self):
        for name in ("current", "next"):
            key = VECTORS["keys"][name]
            der = base64.b64decode(key["publicKeySpkiBase64"])
            self.assertEqual(device_auth.compute_kid(der), key["kid"])

    def test_a_key_whose_kid_isnt_its_own_is_left_out(self):
        parsed = json.loads(VECTORS["keysRecord"]["content"])
        parsed["keys"][0]["kid"] = "0000000000000000"
        record = device_auth.parse_keys_record(json.dumps(parsed))
        self.assertEqual(len(record.keys), 1)
        self.assertEqual(record.skipped, 1)

    def test_a_key_with_an_algorithm_not_implemented_is_ignored(self):
        parsed = json.loads(VECTORS["keysRecord"]["content"])
        parsed["keys"][0]["alg"] = "RS256"
        record = device_auth.parse_keys_record(json.dumps(parsed))
        self.assertEqual(len(record.keys), 1)
        self.assertEqual(record.skipped, 0)
        self.assertEqual(record.unknown_algorithms, frozenset({"RS256"}))

    def test_an_es256_key_beside_an_unknown_algorithm_still_works(self):
        parsed = json.loads(VECTORS["keysRecord"]["content"])
        parsed["keys"].append(
            {
                "alg": "ML-DSA-65",
                "kid": "pq00000000000001",
                "key": base64.b64encode(b"a future public key").decode(),
                "nbf": parsed["keys"][0]["nbf"],
                "exp": parsed["keys"][0]["exp"],
                "params": {"unknown": True},
            }
        )
        record = device_auth.parse_keys_record(json.dumps(parsed))
        self.assertEqual(record.skipped, 0)
        self.assertEqual(record.unknown_algorithms, frozenset({"ML-DSA-65"}))
        self.assertEqual(
            {key.kid for key in record.keys},
            {VECTORS["keys"]["current"]["kid"], VECTORS["keys"]["next"]["kid"]},
        )
        self.assertTrue(all(key.alg == device_auth.ALG_ES256 for key in record.keys))
        token = next(v for v in VECTORS["tokens"] if v["name"] == "valid-plain")["token"]
        claims = device_auth.check_token(token, record.keys, NOW, SITE_ID)
        self.assertEqual(claims.site_id, SITE_ID)

    def test_an_entry_without_an_algorithm_is_left_out(self):
        parsed = json.loads(VECTORS["keysRecord"]["content"])
        del parsed["keys"][0]["alg"]
        parsed["keys"].append("nonsense")
        record = device_auth.parse_keys_record(json.dumps(parsed))
        self.assertEqual(len(record.keys), 1)
        self.assertEqual(record.skipped, 2)

    def test_not_a_keys_record(self):
        with self.assertRaises(ValueError):
            device_auth.parse_keys_record('{"cb": "https://example.com"}')
        with self.assertRaises(ValueError):
            device_auth.parse_keys_record("not json")

    def test_only_the_current_key_is_valid_now(self):
        valid = [key.kid for key in _keys() if key.valid_at(NOW)]
        self.assertEqual(valid, [VECTORS["keys"]["current"]["kid"]])


class TokenTest(unittest.TestCase):
    def test_vectors(self):
        keys = _keys()
        for vector in VECTORS["tokens"]:
            with self.subTest(vector["name"]):
                if vector["expect"] == "valid":
                    claims = device_auth.check_token(vector["token"], keys, NOW, SITE_ID)
                    self.assertEqual(claims.site_id, SITE_ID)
                    self.assertEqual(claims.admin, vector["name"] == "valid-admin")
                else:
                    with self.assertRaises(device_auth.TokenError):
                        device_auth.check_token(vector["token"], keys, NOW, SITE_ID)

    def test_another_site_is_wrong_site(self):
        token = next(v for v in VECTORS["tokens"] if v["name"] == "wrong-site")["token"]
        with self.assertRaises(device_auth.TokenError) as raised:
            device_auth.check_token(token, _keys(), NOW, SITE_ID)
        self.assertEqual(raised.exception.code, device_auth.ERROR_WRONG_SITE)

    def test_any_site_without_a_linked_site(self):
        token = next(v for v in VECTORS["tokens"] if v["name"] == "wrong-site")["token"]
        claims = device_auth.check_token(token, _keys(), NOW, None)
        self.assertNotEqual(claims.site_id, SITE_ID)

    def test_valid_token_claims(self):
        token = next(v for v in VECTORS["tokens"] if v["name"] == "valid-plain")["token"]
        claims = device_auth.check_token(token, _keys(), NOW, SITE_ID)
        self.assertEqual(claims.profile_id, VECTORS["device"]["profileId"])
        self.assertEqual(claims.device_id, VECTORS["device"]["deviceId"])

    def test_not_a_token(self):
        for value in (None, "", 42, "not-a-jwt"):
            with self.subTest(value), self.assertRaises(device_auth.TokenError):
                device_auth.check_token(value, _keys(), NOW, SITE_ID)

    def test_only_the_production_issuer_is_accepted(self):
        self.assertEqual(device_auth.TOKEN_ISSUER, VECTORS["issuer"])
        token = self._signed(
            self._claims(iss="https://device-control-backend.testing.gnt.betterna.me")
        )
        with self.assertRaises(device_auth.TokenError) as raised:
            device_auth.check_token(token, _keys(), NOW, SITE_ID)
        self.assertEqual(raised.exception.code, device_auth.ERROR_INVALID_TOKEN)

    def _signed(self, claims, alg="ES256", kid=None):
        import jwt

        return jwt.encode(
            claims,
            VECTORS["keys"]["current"]["privateKeyPkcs8Pem"],
            algorithm=alg,
            headers={"kid": kid or VECTORS["keys"]["current"]["kid"]},
        )

    def _claims(self, **extra):
        device = VECTORS["device"]
        return {
            "siteId": SITE_ID,
            "profileId": device["profileId"],
            "deviceId": device["deviceId"],
            "iss": VECTORS["issuer"],
            "aud": VECTORS["audience"],
            "iat": NOW - 60,
            "exp": NOW + 3600,
            "encryptionPublicKey": device["encryptionKey"]["publicKeySpkiBase64"],
            **extra,
        }

    def test_unknown_claims_are_ignored(self):
        token = self._signed(
            self._claims(
                encryptionPublicKeys=[{"alg": "ML-KEM-768", "key": "AAAA"}],
                futureClaim={"anything": 1},
            )
        )
        claims = device_auth.check_token(token, _keys(), NOW, SITE_ID)
        self.assertEqual(
            claims.encryption_public_key,
            VECTORS["device"]["encryptionKey"]["publicKeySpkiBase64"],
        )

    def test_a_header_alg_that_isnt_its_keys_is_refused(self):
        token = next(v for v in VECTORS["tokens"] if v["name"] == "valid-plain")["token"]
        current = next(
            key for key in _keys() if key.kid == VECTORS["keys"]["current"]["kid"]
        )
        # The same key, listed for another algorithm: the ES256 token no longer
        # matches its key's algorithm.
        relisted = device_auth.SigningKey(
            kid=current.kid,
            alg="ES999",
            public_key=current.public_key,
            nbf=current.nbf,
            exp=current.exp,
        )
        with self.assertRaises(device_auth.TokenError) as raised:
            device_auth.check_token(token, [relisted], NOW, SITE_ID)
        self.assertEqual(raised.exception.code, device_auth.ERROR_INVALID_TOKEN)

    def test_a_header_alg_not_implemented_is_refused(self):
        import jwt

        valid = next(v for v in VECTORS["tokens"] if v["name"] == "valid-plain")["token"]
        _, payload, signature = valid.split(".")
        for alg in ("ES384", "RS256", "HS256", "none", "ML-DSA-65"):
            header = jwt.utils.base64url_encode(
                json.dumps(
                    {"alg": alg, "typ": "JWT", "kid": VECTORS["keys"]["current"]["kid"]}
                ).encode()
            ).decode()
            with self.subTest(alg), self.assertRaises(device_auth.TokenError):
                device_auth.check_token(
                    f"{header}.{payload}.{signature}", _keys(), NOW, SITE_ID
                )

    def test_kid_from_header(self):
        token = next(v for v in VECTORS["tokens"] if v["name"] == "valid-plain")["token"]
        self.assertEqual(device_auth.token_kid(token), VECTORS["keys"]["current"]["kid"])
        self.assertIsNone(device_auth.token_kid("not-a-jwt"))


def _one_off_key(scalar_hex):
    return ec.derive_private_key(int(scalar_hex, 16), ec.SECP256R1())


def _device_private_key():
    return serialization.load_pem_private_key(
        VECTORS["device"]["encryptionKey"]["privateKeyPkcs8Pem"].encode(), password=None
    )


def _vector_registration(admin=False, device_id=None):
    device = VECTORS["device"]
    return device_auth.Registration(
        profile_id=device["profileId"],
        device_id=device_id or device["deviceId"],
        api_key_hash="0" * 64,
        public_key=device["encryptionKey"]["publicKeySpkiBase64"],
        key_id=device["keyId"],
        admin=admin,
        registered_at=NOW,
    )


def _envelope_vector(name):
    return next(v for v in VECTORS["envelopes"] if v["name"] == name)


def _sealed_vector(name):
    return next(v for v in VECTORS["sealedPayloads"] if v["name"] == name)


class PublicKeyTest(unittest.TestCase):
    def test_key_id_follows_the_rule(self):
        public_key = device_auth.parse_public_key(
            VECTORS["device"]["encryptionKey"]["publicKeySpkiBase64"]
        )
        self.assertIsNotNone(public_key)
        self.assertEqual(device_auth.public_key_id(public_key), VECTORS["device"]["keyId"])

    def test_not_a_p256_public_key(self):
        token = next(v for v in VECTORS["tokens"] if v["name"] == "valid-public-key-not-p256")
        claims = device_auth.check_token(token["token"], _keys(), NOW, SITE_ID)
        self.assertIsNotNone(claims.encryption_public_key)
        self.assertIsNone(device_auth.parse_public_key(claims.encryption_public_key))
        for value in (None, "", 42, "not base64!", base64.b64encode(b"junk").decode()):
            with self.subTest(value):
                self.assertIsNone(device_auth.parse_public_key(value))


class RegistrationOutcomeTest(unittest.TestCase):
    def test_vectors(self):
        for vector in VECTORS["tokens"]:
            with self.subTest(vector["name"]):
                book = device_auth.RegistrationBook()
                try:
                    claims = device_auth.check_token(vector["token"], _keys(), NOW, SITE_ID)
                    _, registration = book.register(
                        claims.profile_id,
                        claims.device_id,
                        admin=claims.admin,
                        public_key=claims.encryption_public_key,
                        now=NOW,
                    )
                except (device_auth.TokenError, device_auth.RegistrationError) as err:
                    outcome = err.code
                else:
                    outcome = "accept"
                    self.assertEqual(registration.key_id, VECTORS["device"]["keyId"])
                self.assertEqual(outcome, vector["registration"])


class EnvelopeTest(unittest.TestCase):
    def test_encrypted_envelope_matches_the_vector(self):
        vector = _envelope_vector("ecies-p256")
        inputs = vector["inputs"]
        envelope = device_auth.build_command_envelope(
            json.loads(inputs["plaintext"]),
            _vector_registration(),
            one_off_key=_one_off_key(inputs["oneOffPrivateScalarHex"]),
            nonce=bytes.fromhex(inputs["nonceHex"]),
        )
        expected = vector["envelope"]
        self.assertEqual(envelope["profile_id"], expected["profileId"])
        self.assertEqual(envelope["device_id"], expected["deviceId"])
        self.assertEqual(envelope["id"], expected["id"])
        self.assertEqual(envelope["encryption"], expected["encryption"])
        self.assertEqual(envelope["key_id"], expected["keyId"])
        self.assertEqual(envelope["payload"], expected["payload"])

    def test_intermediates_match_the_vector(self):
        inputs = _envelope_vector("ecies-p256")["inputs"]
        one_off = _one_off_key(inputs["oneOffPrivateScalarHex"])
        recipient = _device_private_key().public_key()
        shared = one_off.exchange(ec.ECDH(), recipient)
        self.assertEqual(shared.hex(), inputs["sharedSecretHex"])
        one_off_point = device_auth._uncompressed_point(one_off.public_key())
        self.assertEqual(one_off_point.hex(), inputs["oneOffPublicKeyUncompressedHex"])
        recipient_point = device_auth._uncompressed_point(recipient)
        self.assertEqual(
            (b"zendo ecies-p256 v1" + one_off_point + recipient_point).hex(),
            inputs["hkdfInfoHex"],
        )
        self.assertEqual(
            device_auth._ecies_key(shared, one_off_point, recipient_point).hex(),
            inputs["aesKeyHex"],
        )

    def test_additional_data_matches_the_vector(self):
        vector = _envelope_vector("ecies-p256")
        envelope = vector["envelope"]
        self.assertEqual(
            device_auth.additional_data(
                envelope["profileId"],
                envelope["deviceId"],
                envelope["id"],
                envelope["encryption"],
                envelope["keyId"],
            ),
            vector["inputs"]["additionalData"].encode(),
        )

    def test_the_device_opens_it(self):
        vector = _envelope_vector("ecies-p256")
        self.assertEqual(
            device_auth.ecies_decrypt(
                _device_private_key(),
                vector["inputs"]["additionalData"].encode(),
                vector["envelope"]["payload"],
            ).decode(),
            vector["inputs"]["plaintext"],
        )

    def test_a_changed_id_fails_to_open(self):
        envelope = _envelope_vector("ecies-p256-tampered-id")["envelope"]
        aad = device_auth.additional_data(
            envelope["profileId"],
            envelope["deviceId"],
            envelope["id"],
            envelope["encryption"],
            envelope["keyId"],
        )
        with self.assertRaises(InvalidTag):
            device_auth.ecies_decrypt(_device_private_key(), aad, envelope["payload"])

    def test_a_truncated_payload_fails_to_open(self):
        with self.assertRaises(ValueError):
            device_auth.ecies_decrypt(
                _device_private_key(), b"", base64.b64encode(b"short").decode()
            )

    def test_must_encrypt_methods_are_the_vectors(self):
        self.assertEqual(
            set(VECTORS["mustEncryptMethods"]), set(device_auth.MUST_ENCRYPT_METHODS)
        )

    def test_other_methods_go_in_clear(self):
        vector = _envelope_vector("none")
        envelope = device_auth.build_command_envelope(vector["request"], _vector_registration())
        self.assertEqual(envelope["encryption"], device_auth.SCHEME_NONE)
        self.assertEqual(envelope["payload"], vector["request"])
        self.assertNotIn("key_id", envelope)

    def test_a_sealed_object_in_the_params_goes_as_it_is(self):
        params = _sealed_vector("command-with-reply-key")["params"]
        request = {"jsonrpc": "2.0", "method": "wifi.configure", "params": params, "id": "x"}
        envelope = device_auth.build_command_envelope(request, _vector_registration())
        self.assertEqual(envelope["payload"]["params"]["sealed"], params["sealed"])

    def test_a_new_one_off_key_each_time(self):
        request = {"jsonrpc": "2.0", "method": "app.lock", "params": {"pin": "1234"}, "id": "x"}
        first = device_auth.build_command_envelope(request, _vector_registration())
        second = device_auth.build_command_envelope(request, _vector_registration())
        self.assertNotEqual(first["payload"][:88], second["payload"][:88])
        self.assertNotEqual(first["payload"], second["payload"])


class MaySendCommandTest(unittest.TestCase):
    """Which commands go to a device that hasn't registered, on an older app."""

    OTHER_METHODS = (
        "app.unlock",
        "app.reload",
        "screen.set",
        "screen.wakeUp",
        "audio.set",
        "screensaver.set",
        "autoBrightness.set",
        "wakeOnApproach.set",
    )

    def test_a_pin_or_an_admin_code_needs_a_registered_device(self):
        for method in ("adminCode.set", "app.lock"):
            with self.subTest(method=method):
                self.assertFalse(device_auth.may_send_command(method, registered=False))
                self.assertTrue(device_auth.may_send_command(method, registered=True))

    def test_every_must_encrypt_method_needs_a_registered_device(self):
        for method in device_auth.MUST_ENCRYPT_METHODS:
            with self.subTest(method=method):
                self.assertFalse(device_auth.may_send_command(method, registered=False))

    def test_every_other_command_goes_either_way(self):
        for method in self.OTHER_METHODS:
            for registered in (False, True):
                with self.subTest(method=method, registered=registered):
                    self.assertTrue(
                        device_auth.may_send_command(method, registered=registered)
                    )


class CommandRefusalTest(unittest.TestCase):
    """Why a command isn't sent, in the order ``send_command`` raises it."""

    Refusal = device_auth.CommandRefusal
    METHODS_NEEDING_REGISTRATION = ("adminCode.set", "app.lock")

    def _refusal(self, method, *, online=True, registered=False, linked=True):
        return device_auth.command_refusal(
            method, online=online, registered=registered, linked=linked
        )

    def test_linked_and_not_registered_needs_a_newer_app(self):
        for method in self.METHODS_NEEDING_REGISTRATION:
            with self.subTest(method=method):
                self.assertIs(self._refusal(method), self.Refusal.APP_UPDATE_REQUIRED)

    def test_not_linked_says_so_instead(self):
        for method in self.METHODS_NEEDING_REGISTRATION:
            with self.subTest(method=method):
                self.assertIs(self._refusal(method, linked=False), self.Refusal.NOT_LINKED)

    def test_not_linked_comes_before_offline(self):
        for method in self.METHODS_NEEDING_REGISTRATION:
            with self.subTest(method=method):
                self.assertIs(
                    self._refusal(method, online=False, linked=False),
                    self.Refusal.NOT_LINKED,
                )

    def test_offline_comes_before_a_newer_app_when_linked(self):
        for method in self.METHODS_NEEDING_REGISTRATION:
            with self.subTest(method=method):
                self.assertIs(self._refusal(method, online=False), self.Refusal.OFFLINE)

    def test_a_registered_device_gets_it_online(self):
        for method in self.METHODS_NEEDING_REGISTRATION:
            for linked in (False, True):
                with self.subTest(method=method, linked=linked):
                    self.assertIsNone(self._refusal(method, registered=True, linked=linked))

    def test_a_registered_device_offline_is_offline(self):
        for method in self.METHODS_NEEDING_REGISTRATION:
            for linked in (False, True):
                with self.subTest(method=method, linked=linked):
                    self.assertIs(
                        self._refusal(method, online=False, registered=True, linked=linked),
                        self.Refusal.OFFLINE,
                    )

    def test_every_other_command_never_depends_on_the_link(self):
        for method in MaySendCommandTest.OTHER_METHODS:
            for registered in (False, True):
                for linked in (False, True):
                    with self.subTest(method=method, registered=registered, linked=linked):
                        self.assertIsNone(
                            self._refusal(method, registered=registered, linked=linked)
                        )
                        self.assertIs(
                            self._refusal(
                                method, online=False, registered=registered, linked=linked
                            ),
                            self.Refusal.OFFLINE,
                        )


class SealedTest(unittest.TestCase):
    def _open(self, private_key, additional_data, data):
        return json.loads(
            device_auth.ecies_decrypt(private_key, additional_data.encode(), data).decode()
        )

    def test_seal_matches_the_vector(self):
        vector = _sealed_vector("command-with-reply-key")
        inputs = vector["inputs"]
        sealed = device_auth.seal(
            json.loads(inputs["plaintext"]),
            _vector_registration(),
            vector["method"],
            one_off_key=_one_off_key(inputs["oneOffPrivateScalarHex"]),
            nonce=bytes.fromhex(inputs["nonceHex"]),
        )
        self.assertEqual(sealed, vector["params"]["sealed"])

    def test_sealed_additional_data_matches_the_vector(self):
        vector = _sealed_vector("command-with-reply-key")
        recipient = vector["recipient"]
        self.assertEqual(
            device_auth.sealed_additional_data(
                recipient["profileId"], recipient["deviceId"], vector["method"]
            ),
            vector["inputs"]["additionalData"].encode(),
        )

    def test_the_device_opens_a_sealed_command(self):
        vector = _sealed_vector("command-with-reply-key")
        self.assertEqual(
            self._open(
                _device_private_key(),
                vector["inputs"]["additionalData"],
                vector["params"]["sealed"]["data"],
            ),
            vector["opened"],
        )

    def test_a_sealed_reply_opens_with_the_reply_key(self):
        vector = _sealed_vector("reply-to-reply-key")
        reply_key = serialization.load_pem_private_key(
            vector["recipientReplyKey"]["privateKeyPkcs8Pem"].encode(), password=None
        )
        self.assertEqual(
            self._open(
                reply_key, vector["inputs"]["additionalData"], vector["result"]["sealed"]["data"]
            ),
            vector["opened"],
        )

    def test_a_sealed_object_doesnt_open_for_another_method_or_device(self):
        for name in ("moved-to-other-method", "sealed-for-other-device"):
            with self.subTest(name):
                vector = _sealed_vector(name)
                recipient = vector["recipient"]
                aad = device_auth.sealed_additional_data(
                    recipient["profileId"], recipient["deviceId"], vector["method"]
                )
                with self.assertRaises(InvalidTag):
                    device_auth.ecies_decrypt(
                        _device_private_key(), aad, vector["params"]["sealed"]["data"]
                    )

    def test_settings_restore_seal_matches_the_vector(self):
        vector = _sealed_vector("settings-restore-answer")
        inputs = vector["inputs"]
        sealed = device_auth.seal(
            json.loads(inputs["plaintext"]),
            _vector_registration(),
            vector["method"],
            one_off_key=_one_off_key(inputs["oneOffPrivateScalarHex"]),
            nonce=bytes.fromhex(inputs["nonceHex"]),
        )
        self.assertEqual(sealed, vector["answer"]["restore"]["sealed"])
        self.assertEqual(vector["method"], device_auth.SEALED_METHOD_SETTINGS_RESTORE)

    def test_a_settings_restore_answer_seals_its_admin_code(self):
        result = {
            "success": True,
            "registered": True,
            "restore": {
                "restore_id": "restore_vectors_1",
                "previous_device_name": "Hallway",
                "settings_backup": {"colorScheme": "dark"},
                "admin_code_status": "set",
                "admin_code": "482915",
            },
        }
        registration = _vector_registration()
        sealed_result = device_auth.seal_settings_restore(result, registration)
        restore = sealed_result["restore"]
        self.assertNotIn("admin_code", restore)
        self.assertEqual(restore["admin_code_status"], "set")
        self.assertEqual(restore["sealed"]["scheme"], device_auth.SCHEME_ECIES_P256)
        aad = device_auth.sealed_additional_data(
            registration.profile_id,
            registration.device_id,
            device_auth.SEALED_METHOD_SETTINGS_RESTORE,
        )
        self.assertEqual(
            self._open(_device_private_key(), aad.decode(), restore["sealed"]["data"]),
            {"admin_code": "482915"},
        )
        self.assertEqual(result["restore"]["admin_code"], "482915")

    def test_a_restore_with_no_code_still_comes_sealed(self):
        result = {
            "success": True,
            "registered": True,
            "restore": {"restore_id": "r", "admin_code_status": "not_set", "admin_code": None},
        }
        restore = device_auth.seal_settings_restore(result, _vector_registration())["restore"]
        aad = device_auth.sealed_additional_data(
            "profile_vectors_1", "device_vectors_1", device_auth.SEALED_METHOD_SETTINGS_RESTORE
        )
        self.assertEqual(
            self._open(_device_private_key(), aad.decode(), restore["sealed"]["data"]),
            {"admin_code": None},
        )

    def test_an_answer_with_no_restore_is_unchanged(self):
        result = {"success": True, "registered": True, "restore": None}
        self.assertEqual(
            device_auth.seal_settings_restore(result, _vector_registration()), result
        )


def _token_claims(name):
    token = next(v for v in VECTORS["tokens"] if v["name"] == name)
    return device_auth.check_token(token["token"], _keys(), NOW, SITE_ID)


class AdminCodeAnswerTest(unittest.TestCase):
    """``zendo/v2/get_managed_device_admin_code``'s answer, sealed to the caller's token."""

    ADDITIONAL_DATA = "sealed\nprofile_vectors_1\ndevice_vectors_1\nadminCode.get"

    def _open(self, additional_data, data):
        return json.loads(
            device_auth.ecies_decrypt(
                _device_private_key(), additional_data.encode(), data
            ).decode()
        )

    def test_the_method_label(self):
        self.assertEqual(device_auth.SEALED_METHOD_ADMIN_CODE_GET, "adminCode.get")

    def test_it_opens_with_the_callers_key_under_its_additional_data(self):
        claims = _token_claims("valid-admin")
        self.assertTrue(claims.admin)
        result = {
            "success": True,
            "status": "set",
            "admin_code": "482915",
            "reported_at": "2026-10-14T09:30:00.123456+00:00",
        }
        answer = device_auth.seal_admin_code_answer(result, claims)
        self.assertEqual(
            answer,
            {
                "success": True,
                "status": "set",
                "reported_at": "2026-10-14T09:30:00.123456+00:00",
                "sealed": answer["sealed"],
            },
        )
        self.assertEqual(answer["sealed"]["scheme"], device_auth.SCHEME_ECIES_P256)
        self.assertEqual(
            device_auth.sealed_additional_data(
                claims.profile_id, claims.device_id, device_auth.SEALED_METHOD_ADMIN_CODE_GET
            ),
            self.ADDITIONAL_DATA.encode(),
        )
        self.assertEqual(
            self._open(self.ADDITIONAL_DATA, answer["sealed"]["data"]),
            {"admin_code": "482915"},
        )
        self.assertEqual(result["admin_code"], "482915")

    def test_no_code_still_comes_sealed(self):
        claims = _token_claims("valid-admin")
        for status, reported_at in (
            ("not_set", "2026-10-14T09:31:12.000000+00:00"),
            ("unknown", None),
        ):
            with self.subTest(status):
                answer = device_auth.seal_admin_code_answer(
                    {
                        "success": True,
                        "status": status,
                        "admin_code": None,
                        "reported_at": reported_at,
                    },
                    claims,
                )
                self.assertNotIn("admin_code", answer)
                self.assertEqual(answer["status"], status)
                self.assertEqual(answer["reported_at"], reported_at)
                self.assertEqual(
                    self._open(self.ADDITIONAL_DATA, answer["sealed"]["data"]),
                    {"admin_code": None},
                )

    def test_it_doesnt_open_for_another_method(self):
        answer = device_auth.seal_admin_code_answer(
            {"success": True, "status": "set", "admin_code": "2468", "reported_at": "t"},
            _token_claims("valid-admin"),
        )
        aad = device_auth.sealed_additional_data(
            "profile_vectors_1", "device_vectors_1", device_auth.SEALED_METHOD_SETTINGS_RESTORE
        )
        with self.assertRaises(InvalidTag):
            device_auth.ecies_decrypt(_device_private_key(), aad, answer["sealed"]["data"])

    def test_a_token_without_a_p256_key_is_public_key_required(self):
        for name in ("valid-no-public-key", "valid-public-key-not-p256"):
            with self.subTest(name):
                claims = _token_claims(name)
                with self.assertRaises(device_auth.TokenError) as err:
                    device_auth.token_public_key(claims)
                self.assertEqual(err.exception.code, device_auth.ERROR_PUBLIC_KEY_REQUIRED)
                with self.assertRaises(device_auth.TokenError) as err:
                    device_auth.seal_admin_code_answer(
                        {"success": True, "status": "unknown", "admin_code": None,
                         "reported_at": None},
                        claims,
                    )
                self.assertEqual(err.exception.code, device_auth.ERROR_PUBLIC_KEY_REQUIRED)

    def test_the_token_key_is_the_vector_devices(self):
        self.assertEqual(
            device_auth.public_key_id(device_auth.token_public_key(_token_claims("valid-admin"))),
            VECTORS["device"]["keyId"],
        )


class RegistrationBookTest(unittest.TestCase):
    PUBLIC_KEY = VECTORS["device"]["encryptionKey"]["publicKeySpkiBase64"]

    def _register(self, book, device_id="d1", admin=False, public_key=PUBLIC_KEY):
        return book.register("p1", device_id, admin=admin, public_key=public_key, now=NOW)

    def test_register_and_find(self):
        book = device_auth.RegistrationBook()
        api_key, registration = self._register(book)
        self.assertTrue(device_auth.is_api_key(api_key))
        self.assertEqual(registration.public_key, self.PUBLIC_KEY)
        self.assertEqual(registration.key_id, VECTORS["device"]["keyId"])
        self.assertEqual(registration.scheme, device_auth.SCHEME_ECIES_P256)
        self.assertEqual(book.find_by_api_key(api_key), registration)
        self.assertEqual(book.find_by_device("p1", "d1"), registration)
        self.assertNotEqual(registration.api_key_hash, api_key)

    def test_a_public_key_is_required(self):
        not_p256 = next(
            v for v in VECTORS["tokens"] if v["name"] == "valid-public-key-not-p256"
        )
        claims = device_auth.check_token(not_p256["token"], _keys(), NOW, SITE_ID)
        for value in (None, "", "not base64!", claims.encryption_public_key):
            with self.subTest(value), self.assertRaises(device_auth.RegistrationError) as raised:
                self._register(device_auth.RegistrationBook(), public_key=value)
            self.assertEqual(raised.exception.code, device_auth.ERROR_PUBLIC_KEY_REQUIRED)

    def test_registering_again_replaces_the_api_key_and_the_public_key(self):
        other_key = base64.b64encode(
            ec.generate_private_key(ec.SECP256R1())
            .public_key()
            .public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).decode()
        book = device_auth.RegistrationBook()
        old_key, old = self._register(book)
        new_key, new = self._register(book, public_key=other_key)
        self.assertIsNone(book.find_by_api_key(old_key))
        self.assertEqual(book.find_by_api_key(new_key), new)
        self.assertEqual(new.public_key, other_key)
        self.assertNotEqual(old.key_id, new.key_id)
        self.assertEqual(len(book.registrations()), 1)

    def test_a_revoked_device_cant_register_again(self):
        book = device_auth.RegistrationBook()
        api_key, _ = self._register(book)
        self.assertIsNotNone(book.revoke("p1", "d1", NOW))
        self.assertIsNone(book.find_by_api_key(api_key))
        with self.assertRaises(device_auth.RegistrationError) as raised:
            self._register(book)
        self.assertEqual(raised.exception.code, device_auth.ERROR_DEVICE_REVOKED)

    def test_revoking_an_unregistered_device_still_keeps_its_tombstone(self):
        book = device_auth.RegistrationBook()
        self.assertIsNone(book.revoke("p1", "d2", NOW))
        self.assertTrue(book.is_tombstoned("p1", "d2"))

    def test_a_new_device_id_registers(self):
        book = device_auth.RegistrationBook()
        book.revoke("p1", "d1", NOW)
        _, registration = self._register(book, device_id="d9")
        self.assertEqual(registration.device_id, "d9")

    def test_admin_registered_stays_on(self):
        book = device_auth.RegistrationBook()
        self.assertFalse(book.admin_registered)
        self._register(book, admin=True)
        self.assertTrue(book.admin_registered)
        book.revoke("p1", "d1", NOW)
        self.assertTrue(book.admin_registered)

    def test_unknown_or_malformed_api_keys(self):
        book = device_auth.RegistrationBook()
        self._register(book)
        for value in (None, "", 42, "x" * 43, "short", device_auth.new_api_key()):
            with self.subTest(value):
                self.assertIsNone(book.find_by_api_key(value))

    def test_round_trip(self):
        book = device_auth.RegistrationBook()
        api_key, registration = self._register(book, admin=True)
        book.revoke("p1", "gone", NOW)
        restored = device_auth.RegistrationBook.from_data(
            json.loads(json.dumps(book.to_data()))
        )
        self.assertEqual(restored.find_by_api_key(api_key), registration)
        self.assertTrue(restored.is_tombstoned("p1", "gone"))
        self.assertTrue(restored.admin_registered)

    def test_restoring_drops_what_isnt_valid(self):
        data = {
            "registrations": [{"profile_id": "p1"}, "nonsense"],
            "tombstones": [{"profile_id": "p1"}, None],
            "admin_registered": "yes",
        }
        restored = device_auth.RegistrationBook.from_data(data)
        self.assertEqual(restored.registrations(), [])
        self.assertFalse(restored.admin_registered)
        self.assertEqual(device_auth.RegistrationBook.from_data(None).registrations(), [])

    def test_restoring_drops_a_registration_whose_key_id_isnt_its_keys(self):
        book = device_auth.RegistrationBook()
        self._register(book)
        data = book.to_data()
        data["registrations"][0]["key_id"] = "0000000000000000"
        self.assertEqual(device_auth.RegistrationBook.from_data(data).registrations(), [])

    def test_restoring_drops_an_older_registration_shape(self):
        data = {
            "registrations": [
                {
                    "profile_id": "p1",
                    "device_id": "d1",
                    "api_key_hash": "0" * 64,
                    "scheme": "aes-256-gcm",
                    "key_id": "1a2b3c4d",
                    "encryption_key": base64.b64encode(b"k" * 32).decode(),
                    "admin": False,
                    "registered_at": NOW,
                }
            ]
        }
        self.assertEqual(device_auth.RegistrationBook.from_data(data).registrations(), [])

    def test_a_tombstoned_registration_isnt_restored(self):
        book = device_auth.RegistrationBook()
        self._register(book)
        data = book.to_data()
        data["tombstones"] = [{"profile_id": "p1", "device_id": "d1", "revoked_at": NOW}]
        self.assertEqual(device_auth.RegistrationBook.from_data(data).registrations(), [])


class SchemeTest(unittest.TestCase):
    def test_the_first_supported_scheme(self):
        self.assertEqual(
            device_auth.choose_scheme({"schemes": ["ecies-p256"]}),
            device_auth.SCHEME_ECIES_P256,
        )

    def test_unknown_schemes_are_skipped(self):
        self.assertEqual(
            device_auth.choose_scheme({"schemes": ["hpke-ml-kem-768", 42, "ecies-p256"]}),
            device_auth.SCHEME_ECIES_P256,
        )

    def test_nothing_in_common(self):
        for encryption in ({"schemes": ["hpke-ml-kem-768"]}, {"schemes": []}, {"schemes": "x"}, "x"):
            with self.subTest(encryption):
                self.assertIsNone(device_auth.choose_scheme(encryption))

    def test_no_encryption_or_no_schemes(self):
        for encryption in (None, {}):
            with self.subTest(encryption):
                self.assertEqual(
                    device_auth.choose_scheme(encryption), device_auth.SCHEME_ECIES_P256
                )


class TokenLabelTest(unittest.TestCase):
    NAME = (
        "Zendo token - DO NOT DELETE - https://myzendo.com/support/ha-auth-tokens - "
        'bngnt1:{"s":"site1","p":"p1","d":"d1"}'
    )

    def test_label_of_a_device_token(self):
        self.assertTrue(device_auth.is_device_token_label(self.NAME, "site1", "p1", "d1"))
        self.assertFalse(device_auth.is_device_token_label(self.NAME, "site1", "p1", "d2"))
        self.assertFalse(device_auth.is_device_token_label(self.NAME, "site2", "p1", "d1"))

    def test_invitation_and_other_tokens(self):
        invitation = 'Zendo token - bngnt1:{"s":"site1","p":"p1","i":"inv1"}'
        self.assertFalse(device_auth.is_device_token_label(invitation, "site1", "p1", "d1"))
        for name in (None, "My phone", "bngnt1:not json"):
            with self.subTest(name):
                self.assertFalse(device_auth.is_device_token_label(name, "site1", "p1", "d1"))


class SiteLinkTest(unittest.TestCase):
    def test_not_linked(self):
        self.assertEqual(device_auth.site_link(None, SITE_ID), "not_linked")

    def test_linked_to_the_tokens_site(self):
        self.assertEqual(device_auth.site_link(SITE_ID, SITE_ID), "this_property")

    def test_linked_to_another_site(self):
        self.assertEqual(device_auth.site_link(SITE_ID, "another-site"), "another_property")
        self.assertEqual(device_auth.site_link("another-site", SITE_ID), "another_property")


class LinkRetryIntervalTest(unittest.TestCase):
    def test_the_first_wait_is_a_minute(self):
        self.assertEqual(device_auth.link_retry_interval(1), 60)

    def test_the_wait_doubles_up_to_an_hour(self):
        self.assertEqual(
            [device_auth.link_retry_interval(n) for n in (1, 2, 3, 4, 5, 6, 7, 8, 40)],
            [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600],
        )

    def test_it_never_waits_less_than_the_first_wait(self):
        self.assertEqual(device_auth.link_retry_interval(0), 60)


class PushTokenSerialTest(unittest.TestCase):
    KEY = "x" * 32

    def _token(self, **claims):
        import jwt

        return jwt.encode({"siteId": SITE_ID, **claims}, self.KEY, algorithm="HS256")

    def test_reads_the_serial_number_claim(self):
        self.assertEqual(
            device_auth.push_token_serial(self._token(jti="sn-1")), "sn-1"
        )

    def test_reads_it_without_verifying_the_token(self):
        for claims in (
            {"exp": NOW - 3600},
            {"nbf": 4102444800},
            {"iat": 4102444800},
            {"aud": "someone-else"},
            {"iss": "https://elsewhere.example.com"},
        ):
            with self.subTest(claims):
                self.assertEqual(
                    device_auth.push_token_serial(self._token(jti="sn-1", **claims)),
                    "sn-1",
                )
        tampered = self._token(jti="sn-1").rsplit(".", 1)[0] + ".AAAA"
        self.assertEqual(device_auth.push_token_serial(tampered), "sn-1")

    def test_null_without_a_token(self):
        for value in (None, "", 42, {"jti": "sn-1"}):
            with self.subTest(value):
                self.assertIsNone(device_auth.push_token_serial(value))

    def test_null_when_the_token_cant_be_read(self):
        not_an_object = ".".join(
            base64.urlsafe_b64encode(part).rstrip(b"=").decode()
            for part in (b'{"alg":"HS256"}', b'["jti"]', b"sig")
        )
        for value in ("not-a-jwt", "a.b.c", "...", not_an_object, "\ud800.\ud800.\ud800"):
            with self.subTest(value):
                self.assertIsNone(device_auth.push_token_serial(value))

    def test_null_when_the_claim_isnt_a_string(self):
        self.assertIsNone(device_auth.push_token_serial(self._token()))
        for serial in (None, "", 42, ["sn-1"]):
            with self.subTest(serial):
                self.assertIsNone(
                    device_auth.push_token_serial(self._token(jti=serial))
                )


class AdminCodeLookupTest(unittest.TestCase):
    STORED = {"status": "set", "admin_code": "246810", "reported_at": "2026-10-14T09:30:00+00:00"}
    UNKNOWN = {"status": "unknown", "admin_code": None, "reported_at": None}

    def _book(self):
        book = device_auth.RegistrationBook()
        book.register(
            "p1",
            "d1",
            admin=False,
            public_key=VECTORS["device"]["encryptionKey"]["publicKeySpkiBase64"],
            now=NOW,
        )
        return book

    def _lookup(self, book, profile_id, device_id, stored=None):
        return device_auth.admin_code_lookup_for_attached_device(
            stored or self.STORED, book, profile_id, device_id
        )

    def test_a_registered_device_reads_what_the_store_holds(self):
        self.assertEqual(self._lookup(self._book(), "p1", "d1"), self.STORED)
        not_set = {**self.STORED, "status": "not_set", "admin_code": None}
        self.assertEqual(self._lookup(self._book(), "p1", "d1", not_set), not_set)

    def test_a_device_that_hasnt_registered_reads_as_unknown(self):
        self.assertEqual(self._lookup(self._book(), "p1", "d2"), self.UNKNOWN)
        self.assertEqual(self._lookup(self._book(), "p2", "d1"), self.UNKNOWN)
        self.assertEqual(
            self._lookup(device_auth.RegistrationBook(), "p1", "d1"), self.UNKNOWN
        )

    def test_a_revoked_device_reads_as_unknown(self):
        book = self._book()
        book.revoke("p1", "d1", NOW)
        self.assertEqual(self._lookup(book, "p1", "d1"), self.UNKNOWN)

    def test_the_answer_isnt_the_shared_constant(self):
        answer = self._lookup(self._book(), "p1", "d2")
        answer["status"] = "set"
        self.assertEqual(device_auth.UNKNOWN_ADMIN_CODE_LOOKUP, self.UNKNOWN)

    def test_it_seals_a_null_code(self):
        device = VECTORS["device"]
        private_key = serialization.load_pem_private_key(
            device["encryptionKey"]["privateKeyPkcs8Pem"].encode(), password=None
        )
        claims = device_auth.TokenClaims(
            site_id=SITE_ID,
            profile_id="admin-profile",
            device_id="admin-device",
            admin=True,
            encryption_public_key=device["encryptionKey"]["publicKeySpkiBase64"],
        )
        answer = device_auth.seal_admin_code_answer(
            {"success": True, **self._lookup(self._book(), "p1", "d2")}, claims
        )
        self.assertEqual(answer["status"], "unknown")
        self.assertIsNone(answer["reported_at"])
        self.assertNotIn("admin_code", answer)
        opened = device_auth.ecies_decrypt(
            private_key,
            device_auth.sealed_additional_data(
                "admin-profile", "admin-device", device_auth.SEALED_METHOD_ADMIN_CODE_GET
            ),
            answer["sealed"]["data"],
        )
        self.assertEqual(json.loads(opened), {"admin_code": None})


class HeldEncryptionKeyTest(unittest.TestCase):
    """Which site config encryption key is held: the store's, the helper's, or none."""

    def _unread(self):
        self.fail("The helper was read with a key stored")

    def test_the_stored_key_wins_and_the_helper_isnt_read(self):
        self.assertEqual(device_auth.held_encryption_key("stored", self._unread), "stored")

    def test_with_none_stored_the_helpers_key_is_held(self):
        self.assertEqual(device_auth.held_encryption_key(None, lambda: "from helper"), "from helper")

    def test_with_none_stored_and_no_key_in_the_helper_none_is_held(self):
        for state in (None, "", "unknown", "unavailable"):
            with self.subTest(state):
                self.assertIsNone(device_auth.held_encryption_key(None, lambda s=state: s))

    def test_the_helpers_states_that_arent_a_key(self):
        for state in (None, "", "unknown", "unavailable", 42, ["key"], {"key": "k"}):
            with self.subTest(state):
                self.assertIsNone(device_auth.helper_encryption_key(state))

    def test_the_helpers_state_that_is_a_key(self):
        for state in ("k", "Unknown", "a" * 128):
            with self.subTest(state):
                self.assertEqual(device_auth.helper_encryption_key(state), state)

    def test_reading_the_store(self):
        self.assertEqual(device_auth.restored_encryption_key({"key": "stored"}), "stored")

    def test_reading_a_store_with_no_key(self):
        for data in (None, "stored", [], {}, {"key": None}, {"key": ""}, {"key": 42}):
            with self.subTest(data):
                self.assertIsNone(device_auth.restored_encryption_key(data))


class EncryptionKeyAnswerTest(unittest.TestCase):
    """The answers of ``zendo/v2/get_encryption_key`` and ``zendo/v2/claim_encryption_key``."""

    ADDITIONAL_DATA = "sealed\nprofile_vectors_1\ndevice_vectors_1\nencryptionKey.get"

    def _open(self, answer):
        return json.loads(
            device_auth.ecies_decrypt(
                _device_private_key(), self.ADDITIONAL_DATA.encode(), answer["sealed"]["data"]
            ).decode()
        )

    def test_the_method_label(self):
        self.assertEqual(device_auth.SEALED_METHOD_ENCRYPTION_KEY_GET, "encryptionKey.get")

    def test_with_no_key_held_the_answer_matches_the_vector(self):
        vector = _sealed_vector("encryption-key-get-null")
        self.assertEqual(vector["method"], device_auth.SEALED_METHOD_ENCRYPTION_KEY_GET)
        self.assertEqual(
            device_auth.encryption_key_answer(None, _vector_registration()), vector["answer"]
        )

    def test_a_read_opens_with_the_registrations_key(self):
        answer = device_auth.encryption_key_answer("the key", _vector_registration())
        self.assertEqual(sorted(answer), ["sealed", "success"])
        self.assertIs(answer["success"], True)
        self.assertEqual(answer["sealed"]["scheme"], device_auth.SCHEME_ECIES_P256)
        self.assertEqual(self._open(answer), {"key": "the key"})

    def test_a_claim_opens_with_the_tokens_key_under_the_tokens_ids(self):
        claims = _token_claims("valid-admin")
        answer = device_auth.claimed_encryption_key_answer("the key", claims)
        self.assertEqual(sorted(answer), ["sealed", "success"])
        self.assertIs(answer["success"], True)
        self.assertEqual(
            device_auth.sealed_additional_data(
                claims.profile_id, claims.device_id, device_auth.SEALED_METHOD_ENCRYPTION_KEY_GET
            ),
            self.ADDITIONAL_DATA.encode(),
        )
        self.assertEqual(self._open(answer), {"key": "the key"})

    def test_neither_opens_for_another_method_or_device(self):
        answers = (
            device_auth.encryption_key_answer("the key", _vector_registration()),
            device_auth.claimed_encryption_key_answer("the key", _token_claims("valid-admin")),
        )
        device = VECTORS["device"]
        for aad in (
            device_auth.sealed_additional_data(
                device["profileId"], device["deviceId"], device_auth.SEALED_METHOD_ADMIN_CODE_GET
            ),
            device_auth.sealed_additional_data(
                device["profileId"], "another-device", device_auth.SEALED_METHOD_ENCRYPTION_KEY_GET
            ),
        ):
            for answer in answers:
                with self.assertRaises(InvalidTag):
                    device_auth.ecies_decrypt(_device_private_key(), aad, answer["sealed"]["data"])

    def test_a_claim_with_a_token_without_a_p256_key_is_public_key_required(self):
        for name in ("valid-no-public-key", "valid-public-key-not-p256"):
            with self.subTest(name):
                with self.assertRaises(device_auth.TokenError) as err:
                    device_auth.claimed_encryption_key_answer("the key", _token_claims(name))
                self.assertEqual(err.exception.code, device_auth.ERROR_PUBLIC_KEY_REQUIRED)
                self.assertNotIn("the key", str(err.exception))


if __name__ == "__main__":
    unittest.main()
