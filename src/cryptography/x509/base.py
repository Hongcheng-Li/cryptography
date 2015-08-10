# This file is dual licensed under the terms of the Apache License, Version
# 2.0, and the BSD License. See the LICENSE file in the root of this repository
# for complete details.

from __future__ import absolute_import, division, print_function

import abc
import datetime
import hashlib
import ipaddress
from email.utils import parseaddr
from enum import Enum

import idna

from pyasn1.codec.der import decoder
from pyasn1.type import namedtype, univ

import six

from six.moves import urllib_parse

from cryptography import utils
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, rsa
from cryptography.x509.name import Name
from cryptography.x509.oid import (
    ExtensionOID, OID_CA_ISSUERS, OID_OCSP, ObjectIdentifier
)


class _SubjectPublicKeyInfo(univ.Sequence):
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('algorithm', univ.Sequence()),
        namedtype.NamedType('subjectPublicKey', univ.BitString())
    )


def _key_identifier_from_public_key(public_key):
    # This is a very slow way to do this.
    serialized = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo
    )
    spki, remaining = decoder.decode(
        serialized, asn1Spec=_SubjectPublicKeyInfo()
    )
    assert not remaining
    # the univ.BitString object is a tuple of bits. We need bytes and
    # pyasn1 really doesn't want to give them to us. To get it we'll
    # build an integer and convert that to bytes.
    bits = 0
    for bit in spki.getComponentByName("subjectPublicKey"):
        bits = bits << 1 | bit

    data = utils.int_to_bytes(bits)
    return hashlib.sha1(data).digest()


_GENERAL_NAMES = {
    0: "otherName",
    1: "rfc822Name",
    2: "dNSName",
    3: "x400Address",
    4: "directoryName",
    5: "ediPartyName",
    6: "uniformResourceIdentifier",
    7: "iPAddress",
    8: "registeredID",
}

_UNIX_EPOCH = datetime.datetime(1970, 1, 1)


class Version(Enum):
    v1 = 0
    v3 = 2


def load_pem_x509_certificate(data, backend):
    return backend.load_pem_x509_certificate(data)


def load_der_x509_certificate(data, backend):
    return backend.load_der_x509_certificate(data)


def load_pem_x509_csr(data, backend):
    return backend.load_pem_x509_csr(data)


def load_der_x509_csr(data, backend):
    return backend.load_der_x509_csr(data)


class InvalidVersion(Exception):
    def __init__(self, msg, parsed_version):
        super(InvalidVersion, self).__init__(msg)
        self.parsed_version = parsed_version


class DuplicateExtension(Exception):
    def __init__(self, msg, oid):
        super(DuplicateExtension, self).__init__(msg)
        self.oid = oid


class UnsupportedExtension(Exception):
    def __init__(self, msg, oid):
        super(UnsupportedExtension, self).__init__(msg)
        self.oid = oid


class ExtensionNotFound(Exception):
    def __init__(self, msg, oid):
        super(ExtensionNotFound, self).__init__(msg)
        self.oid = oid


class UnsupportedGeneralNameType(Exception):
    def __init__(self, msg, type):
        super(UnsupportedGeneralNameType, self).__init__(msg)
        self.type = type


class Extensions(object):
    def __init__(self, extensions):
        self._extensions = extensions

    def get_extension_for_oid(self, oid):
        for ext in self:
            if ext.oid == oid:
                return ext

        raise ExtensionNotFound("No {0} extension was found".format(oid), oid)

    def __iter__(self):
        return iter(self._extensions)

    def __len__(self):
        return len(self._extensions)


class Extension(object):
    def __init__(self, oid, critical, value):
        if not isinstance(oid, ObjectIdentifier):
            raise TypeError(
                "oid argument must be an ObjectIdentifier instance."
            )

        if not isinstance(critical, bool):
            raise TypeError("critical must be a boolean value")

        self._oid = oid
        self._critical = critical
        self._value = value

    oid = utils.read_only_property("_oid")
    critical = utils.read_only_property("_critical")
    value = utils.read_only_property("_value")

    def __repr__(self):
        return ("<Extension(oid={0.oid}, critical={0.critical}, "
                "value={0.value})>").format(self)

    def __eq__(self, other):
        if not isinstance(other, Extension):
            return NotImplemented

        return (
            self.oid == other.oid and
            self.critical == other.critical and
            self.value == other.value
        )

    def __ne__(self, other):
        return not self == other


@six.add_metaclass(abc.ABCMeta)
class ExtensionType(object):
    @abc.abstractproperty
    def oid(self):
        """
        Returns the oid associated with the given extension type.
        """


@utils.register_interface(ExtensionType)
class ExtendedKeyUsage(object):
    oid = ExtensionOID.EXTENDED_KEY_USAGE

    def __init__(self, usages):
        if not all(isinstance(x, ObjectIdentifier) for x in usages):
            raise TypeError(
                "Every item in the usages list must be an ObjectIdentifier"
            )

        self._usages = usages

    def __iter__(self):
        return iter(self._usages)

    def __len__(self):
        return len(self._usages)

    def __repr__(self):
        return "<ExtendedKeyUsage({0})>".format(self._usages)

    def __eq__(self, other):
        if not isinstance(other, ExtendedKeyUsage):
            return NotImplemented

        return self._usages == other._usages

    def __ne__(self, other):
        return not self == other


@utils.register_interface(ExtensionType)
class OCSPNoCheck(object):
    oid = ExtensionOID.OCSP_NO_CHECK


@utils.register_interface(ExtensionType)
class BasicConstraints(object):
    oid = ExtensionOID.BASIC_CONSTRAINTS

    def __init__(self, ca, path_length):
        if not isinstance(ca, bool):
            raise TypeError("ca must be a boolean value")

        if path_length is not None and not ca:
            raise ValueError("path_length must be None when ca is False")

        if (
            path_length is not None and
            (not isinstance(path_length, six.integer_types) or path_length < 0)
        ):
            raise TypeError(
                "path_length must be a non-negative integer or None"
            )

        self._ca = ca
        self._path_length = path_length

    ca = utils.read_only_property("_ca")
    path_length = utils.read_only_property("_path_length")

    def __repr__(self):
        return ("<BasicConstraints(ca={0.ca}, "
                "path_length={0.path_length})>").format(self)

    def __eq__(self, other):
        if not isinstance(other, BasicConstraints):
            return NotImplemented

        return self.ca == other.ca and self.path_length == other.path_length

    def __ne__(self, other):
        return not self == other


@utils.register_interface(ExtensionType)
class KeyUsage(object):
    oid = ExtensionOID.KEY_USAGE

    def __init__(self, digital_signature, content_commitment, key_encipherment,
                 data_encipherment, key_agreement, key_cert_sign, crl_sign,
                 encipher_only, decipher_only):
        if not key_agreement and (encipher_only or decipher_only):
            raise ValueError(
                "encipher_only and decipher_only can only be true when "
                "key_agreement is true"
            )

        self._digital_signature = digital_signature
        self._content_commitment = content_commitment
        self._key_encipherment = key_encipherment
        self._data_encipherment = data_encipherment
        self._key_agreement = key_agreement
        self._key_cert_sign = key_cert_sign
        self._crl_sign = crl_sign
        self._encipher_only = encipher_only
        self._decipher_only = decipher_only

    digital_signature = utils.read_only_property("_digital_signature")
    content_commitment = utils.read_only_property("_content_commitment")
    key_encipherment = utils.read_only_property("_key_encipherment")
    data_encipherment = utils.read_only_property("_data_encipherment")
    key_agreement = utils.read_only_property("_key_agreement")
    key_cert_sign = utils.read_only_property("_key_cert_sign")
    crl_sign = utils.read_only_property("_crl_sign")

    @property
    def encipher_only(self):
        if not self.key_agreement:
            raise ValueError(
                "encipher_only is undefined unless key_agreement is true"
            )
        else:
            return self._encipher_only

    @property
    def decipher_only(self):
        if not self.key_agreement:
            raise ValueError(
                "decipher_only is undefined unless key_agreement is true"
            )
        else:
            return self._decipher_only

    def __repr__(self):
        try:
            encipher_only = self.encipher_only
            decipher_only = self.decipher_only
        except ValueError:
            encipher_only = None
            decipher_only = None

        return ("<KeyUsage(digital_signature={0.digital_signature}, "
                "content_commitment={0.content_commitment}, "
                "key_encipherment={0.key_encipherment}, "
                "data_encipherment={0.data_encipherment}, "
                "key_agreement={0.key_agreement}, "
                "key_cert_sign={0.key_cert_sign}, crl_sign={0.crl_sign}, "
                "encipher_only={1}, decipher_only={2})>").format(
                    self, encipher_only, decipher_only)

    def __eq__(self, other):
        if not isinstance(other, KeyUsage):
            return NotImplemented

        return (
            self.digital_signature == other.digital_signature and
            self.content_commitment == other.content_commitment and
            self.key_encipherment == other.key_encipherment and
            self.data_encipherment == other.data_encipherment and
            self.key_agreement == other.key_agreement and
            self.key_cert_sign == other.key_cert_sign and
            self.crl_sign == other.crl_sign and
            self._encipher_only == other._encipher_only and
            self._decipher_only == other._decipher_only
        )

    def __ne__(self, other):
        return not self == other


@utils.register_interface(ExtensionType)
class AuthorityInformationAccess(object):
    oid = ExtensionOID.AUTHORITY_INFORMATION_ACCESS

    def __init__(self, descriptions):
        if not all(isinstance(x, AccessDescription) for x in descriptions):
            raise TypeError(
                "Every item in the descriptions list must be an "
                "AccessDescription"
            )

        self._descriptions = descriptions

    def __iter__(self):
        return iter(self._descriptions)

    def __len__(self):
        return len(self._descriptions)

    def __repr__(self):
        return "<AuthorityInformationAccess({0})>".format(self._descriptions)

    def __eq__(self, other):
        if not isinstance(other, AuthorityInformationAccess):
            return NotImplemented

        return self._descriptions == other._descriptions

    def __ne__(self, other):
        return not self == other


class AccessDescription(object):
    def __init__(self, access_method, access_location):
        if not (access_method == OID_OCSP or access_method == OID_CA_ISSUERS):
            raise ValueError(
                "access_method must be OID_OCSP or OID_CA_ISSUERS"
            )

        if not isinstance(access_location, GeneralName):
            raise TypeError("access_location must be a GeneralName")

        self._access_method = access_method
        self._access_location = access_location

    def __repr__(self):
        return (
            "<AccessDescription(access_method={0.access_method}, access_locati"
            "on={0.access_location})>".format(self)
        )

    def __eq__(self, other):
        if not isinstance(other, AccessDescription):
            return NotImplemented

        return (
            self.access_method == other.access_method and
            self.access_location == other.access_location
        )

    def __ne__(self, other):
        return not self == other

    access_method = utils.read_only_property("_access_method")
    access_location = utils.read_only_property("_access_location")


@utils.register_interface(ExtensionType)
class CertificatePolicies(object):
    oid = ExtensionOID.CERTIFICATE_POLICIES

    def __init__(self, policies):
        if not all(isinstance(x, PolicyInformation) for x in policies):
            raise TypeError(
                "Every item in the policies list must be a "
                "PolicyInformation"
            )

        self._policies = policies

    def __iter__(self):
        return iter(self._policies)

    def __len__(self):
        return len(self._policies)

    def __repr__(self):
        return "<CertificatePolicies({0})>".format(self._policies)

    def __eq__(self, other):
        if not isinstance(other, CertificatePolicies):
            return NotImplemented

        return self._policies == other._policies

    def __ne__(self, other):
        return not self == other


class PolicyInformation(object):
    def __init__(self, policy_identifier, policy_qualifiers):
        if not isinstance(policy_identifier, ObjectIdentifier):
            raise TypeError("policy_identifier must be an ObjectIdentifier")

        self._policy_identifier = policy_identifier
        if policy_qualifiers and not all(
            isinstance(
                x, (six.text_type, UserNotice)
            ) for x in policy_qualifiers
        ):
            raise TypeError(
                "policy_qualifiers must be a list of strings and/or UserNotice"
                " objects or None"
            )

        self._policy_qualifiers = policy_qualifiers

    def __repr__(self):
        return (
            "<PolicyInformation(policy_identifier={0.policy_identifier}, polic"
            "y_qualifiers={0.policy_qualifiers})>".format(self)
        )

    def __eq__(self, other):
        if not isinstance(other, PolicyInformation):
            return NotImplemented

        return (
            self.policy_identifier == other.policy_identifier and
            self.policy_qualifiers == other.policy_qualifiers
        )

    def __ne__(self, other):
        return not self == other

    policy_identifier = utils.read_only_property("_policy_identifier")
    policy_qualifiers = utils.read_only_property("_policy_qualifiers")


class UserNotice(object):
    def __init__(self, notice_reference, explicit_text):
        if notice_reference and not isinstance(
            notice_reference, NoticeReference
        ):
            raise TypeError(
                "notice_reference must be None or a NoticeReference"
            )

        self._notice_reference = notice_reference
        self._explicit_text = explicit_text

    def __repr__(self):
        return (
            "<UserNotice(notice_reference={0.notice_reference}, explicit_text="
            "{0.explicit_text!r})>".format(self)
        )

    def __eq__(self, other):
        if not isinstance(other, UserNotice):
            return NotImplemented

        return (
            self.notice_reference == other.notice_reference and
            self.explicit_text == other.explicit_text
        )

    def __ne__(self, other):
        return not self == other

    notice_reference = utils.read_only_property("_notice_reference")
    explicit_text = utils.read_only_property("_explicit_text")


class NoticeReference(object):
    def __init__(self, organization, notice_numbers):
        self._organization = organization
        if not isinstance(notice_numbers, list) or not all(
            isinstance(x, int) for x in notice_numbers
        ):
            raise TypeError(
                "notice_numbers must be a list of integers"
            )

        self._notice_numbers = notice_numbers

    def __repr__(self):
        return (
            "<NoticeReference(organization={0.organization!r}, notice_numbers="
            "{0.notice_numbers})>".format(self)
        )

    def __eq__(self, other):
        if not isinstance(other, NoticeReference):
            return NotImplemented

        return (
            self.organization == other.organization and
            self.notice_numbers == other.notice_numbers
        )

    def __ne__(self, other):
        return not self == other

    organization = utils.read_only_property("_organization")
    notice_numbers = utils.read_only_property("_notice_numbers")


@utils.register_interface(ExtensionType)
class SubjectKeyIdentifier(object):
    oid = ExtensionOID.SUBJECT_KEY_IDENTIFIER

    def __init__(self, digest):
        self._digest = digest

    @classmethod
    def from_public_key(cls, public_key):
        return cls(_key_identifier_from_public_key(public_key))

    digest = utils.read_only_property("_digest")

    def __repr__(self):
        return "<SubjectKeyIdentifier(digest={0!r})>".format(self.digest)

    def __eq__(self, other):
        if not isinstance(other, SubjectKeyIdentifier):
            return NotImplemented

        return (
            self.digest == other.digest
        )

    def __ne__(self, other):
        return not self == other


@utils.register_interface(ExtensionType)
class NameConstraints(object):
    oid = ExtensionOID.NAME_CONSTRAINTS

    def __init__(self, permitted_subtrees, excluded_subtrees):
        if permitted_subtrees is not None:
            if not all(
                isinstance(x, GeneralName) for x in permitted_subtrees
            ):
                raise TypeError(
                    "permitted_subtrees must be a list of GeneralName objects "
                    "or None"
                )

            self._validate_ip_name(permitted_subtrees)

        if excluded_subtrees is not None:
            if not all(
                isinstance(x, GeneralName) for x in excluded_subtrees
            ):
                raise TypeError(
                    "excluded_subtrees must be a list of GeneralName objects "
                    "or None"
                )

            self._validate_ip_name(excluded_subtrees)

        if permitted_subtrees is None and excluded_subtrees is None:
            raise ValueError(
                "At least one of permitted_subtrees and excluded_subtrees "
                "must not be None"
            )

        self._permitted_subtrees = permitted_subtrees
        self._excluded_subtrees = excluded_subtrees

    def __eq__(self, other):
        if not isinstance(other, NameConstraints):
            return NotImplemented

        return (
            self.excluded_subtrees == other.excluded_subtrees and
            self.permitted_subtrees == other.permitted_subtrees
        )

    def __ne__(self, other):
        return not self == other

    def _validate_ip_name(self, tree):
        if any(isinstance(name, IPAddress) and not isinstance(
            name.value, (ipaddress.IPv4Network, ipaddress.IPv6Network)
        ) for name in tree):
            raise TypeError(
                "IPAddress name constraints must be an IPv4Network or"
                " IPv6Network object"
            )

    def __repr__(self):
        return (
            u"<NameConstraints(permitted_subtrees={0.permitted_subtrees}, "
            u"excluded_subtrees={0.excluded_subtrees})>".format(self)
        )

    permitted_subtrees = utils.read_only_property("_permitted_subtrees")
    excluded_subtrees = utils.read_only_property("_excluded_subtrees")


@utils.register_interface(ExtensionType)
class CRLDistributionPoints(object):
    oid = ExtensionOID.CRL_DISTRIBUTION_POINTS

    def __init__(self, distribution_points):
        if not all(
            isinstance(x, DistributionPoint) for x in distribution_points
        ):
            raise TypeError(
                "distribution_points must be a list of DistributionPoint "
                "objects"
            )

        self._distribution_points = distribution_points

    def __iter__(self):
        return iter(self._distribution_points)

    def __len__(self):
        return len(self._distribution_points)

    def __repr__(self):
        return "<CRLDistributionPoints({0})>".format(self._distribution_points)

    def __eq__(self, other):
        if not isinstance(other, CRLDistributionPoints):
            return NotImplemented

        return self._distribution_points == other._distribution_points

    def __ne__(self, other):
        return not self == other


class DistributionPoint(object):
    def __init__(self, full_name, relative_name, reasons, crl_issuer):
        if full_name and relative_name:
            raise ValueError(
                "You cannot provide both full_name and relative_name, at "
                "least one must be None."
            )

        if full_name and not all(
            isinstance(x, GeneralName) for x in full_name
        ):
            raise TypeError(
                "full_name must be a list of GeneralName objects"
            )

        if relative_name and not isinstance(relative_name, Name):
            raise TypeError("relative_name must be a Name")

        if crl_issuer and not all(
            isinstance(x, GeneralName) for x in crl_issuer
        ):
            raise TypeError(
                "crl_issuer must be None or a list of general names"
            )

        if reasons and (not isinstance(reasons, frozenset) or not all(
            isinstance(x, ReasonFlags) for x in reasons
        )):
            raise TypeError("reasons must be None or frozenset of ReasonFlags")

        if reasons and (
            ReasonFlags.unspecified in reasons or
            ReasonFlags.remove_from_crl in reasons
        ):
            raise ValueError(
                "unspecified and remove_from_crl are not valid reasons in a "
                "DistributionPoint"
            )

        if reasons and not crl_issuer and not (full_name or relative_name):
            raise ValueError(
                "You must supply crl_issuer, full_name, or relative_name when "
                "reasons is not None"
            )

        self._full_name = full_name
        self._relative_name = relative_name
        self._reasons = reasons
        self._crl_issuer = crl_issuer

    def __repr__(self):
        return (
            "<DistributionPoint(full_name={0.full_name}, relative_name={0.rela"
            "tive_name}, reasons={0.reasons}, crl_issuer={0.crl_is"
            "suer})>".format(self)
        )

    def __eq__(self, other):
        if not isinstance(other, DistributionPoint):
            return NotImplemented

        return (
            self.full_name == other.full_name and
            self.relative_name == other.relative_name and
            self.reasons == other.reasons and
            self.crl_issuer == other.crl_issuer
        )

    def __ne__(self, other):
        return not self == other

    full_name = utils.read_only_property("_full_name")
    relative_name = utils.read_only_property("_relative_name")
    reasons = utils.read_only_property("_reasons")
    crl_issuer = utils.read_only_property("_crl_issuer")


class ReasonFlags(Enum):
    unspecified = "unspecified"
    key_compromise = "keyCompromise"
    ca_compromise = "cACompromise"
    affiliation_changed = "affiliationChanged"
    superseded = "superseded"
    cessation_of_operation = "cessationOfOperation"
    certificate_hold = "certificateHold"
    privilege_withdrawn = "privilegeWithdrawn"
    aa_compromise = "aACompromise"
    remove_from_crl = "removeFromCRL"


@utils.register_interface(ExtensionType)
class InhibitAnyPolicy(object):
    oid = ExtensionOID.INHIBIT_ANY_POLICY

    def __init__(self, skip_certs):
        if not isinstance(skip_certs, six.integer_types):
            raise TypeError("skip_certs must be an integer")

        if skip_certs < 0:
            raise ValueError("skip_certs must be a non-negative integer")

        self._skip_certs = skip_certs

    def __repr__(self):
        return "<InhibitAnyPolicy(skip_certs={0.skip_certs})>".format(self)

    def __eq__(self, other):
        if not isinstance(other, InhibitAnyPolicy):
            return NotImplemented

        return self.skip_certs == other.skip_certs

    def __ne__(self, other):
        return not self == other

    skip_certs = utils.read_only_property("_skip_certs")


@six.add_metaclass(abc.ABCMeta)
class GeneralName(object):
    @abc.abstractproperty
    def value(self):
        """
        Return the value of the object
        """


@utils.register_interface(GeneralName)
class RFC822Name(object):
    def __init__(self, value):
        if not isinstance(value, six.text_type):
            raise TypeError("value must be a unicode string")

        name, address = parseaddr(value)
        parts = address.split(u"@")
        if name or not address:
            # parseaddr has found a name (e.g. Name <email>) or the entire
            # value is an empty string.
            raise ValueError("Invalid rfc822name value")
        elif len(parts) == 1:
            # Single label email name. This is valid for local delivery.
            # No IDNA encoding needed since there is no domain component.
            encoded = address.encode("ascii")
        else:
            # A normal email of the form user@domain.com. Let's attempt to
            # encode the domain component and reconstruct the address.
            encoded = parts[0].encode("ascii") + b"@" + idna.encode(parts[1])

        self._value = value
        self._encoded = encoded

    value = utils.read_only_property("_value")

    def __repr__(self):
        return "<RFC822Name(value={0})>".format(self.value)

    def __eq__(self, other):
        if not isinstance(other, RFC822Name):
            return NotImplemented

        return self.value == other.value

    def __ne__(self, other):
        return not self == other


@utils.register_interface(GeneralName)
class DNSName(object):
    def __init__(self, value):
        if not isinstance(value, six.text_type):
            raise TypeError("value must be a unicode string")

        self._value = value

    value = utils.read_only_property("_value")

    def __repr__(self):
        return "<DNSName(value={0})>".format(self.value)

    def __eq__(self, other):
        if not isinstance(other, DNSName):
            return NotImplemented

        return self.value == other.value

    def __ne__(self, other):
        return not self == other


@utils.register_interface(GeneralName)
class UniformResourceIdentifier(object):
    def __init__(self, value):
        if not isinstance(value, six.text_type):
            raise TypeError("value must be a unicode string")

        parsed = urllib_parse.urlparse(value)
        if not parsed.hostname:
            netloc = ""
        elif parsed.port:
            netloc = (
                idna.encode(parsed.hostname) +
                ":{0}".format(parsed.port).encode("ascii")
            ).decode("ascii")
        else:
            netloc = idna.encode(parsed.hostname).decode("ascii")

        # Note that building a URL in this fashion means it should be
        # semantically indistinguishable from the original but is not
        # guaranteed to be exactly the same.
        uri = urllib_parse.urlunparse((
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment
        )).encode("ascii")

        self._value = value
        self._encoded = uri

    value = utils.read_only_property("_value")

    def __repr__(self):
        return "<UniformResourceIdentifier(value={0})>".format(self.value)

    def __eq__(self, other):
        if not isinstance(other, UniformResourceIdentifier):
            return NotImplemented

        return self.value == other.value

    def __ne__(self, other):
        return not self == other


@utils.register_interface(GeneralName)
class DirectoryName(object):
    def __init__(self, value):
        if not isinstance(value, Name):
            raise TypeError("value must be a Name")

        self._value = value

    value = utils.read_only_property("_value")

    def __repr__(self):
        return "<DirectoryName(value={0})>".format(self.value)

    def __eq__(self, other):
        if not isinstance(other, DirectoryName):
            return NotImplemented

        return self.value == other.value

    def __ne__(self, other):
        return not self == other


@utils.register_interface(GeneralName)
class RegisteredID(object):
    def __init__(self, value):
        if not isinstance(value, ObjectIdentifier):
            raise TypeError("value must be an ObjectIdentifier")

        self._value = value

    value = utils.read_only_property("_value")

    def __repr__(self):
        return "<RegisteredID(value={0})>".format(self.value)

    def __eq__(self, other):
        if not isinstance(other, RegisteredID):
            return NotImplemented

        return self.value == other.value

    def __ne__(self, other):
        return not self == other


@utils.register_interface(GeneralName)
class IPAddress(object):
    def __init__(self, value):
        if not isinstance(
            value,
            (
                ipaddress.IPv4Address,
                ipaddress.IPv6Address,
                ipaddress.IPv4Network,
                ipaddress.IPv6Network
            )
        ):
            raise TypeError(
                "value must be an instance of ipaddress.IPv4Address, "
                "ipaddress.IPv6Address, ipaddress.IPv4Network, or "
                "ipaddress.IPv6Network"
            )

        self._value = value

    value = utils.read_only_property("_value")

    def __repr__(self):
        return "<IPAddress(value={0})>".format(self.value)

    def __eq__(self, other):
        if not isinstance(other, IPAddress):
            return NotImplemented

        return self.value == other.value

    def __ne__(self, other):
        return not self == other


@utils.register_interface(GeneralName)
class OtherName(object):
    def __init__(self, type_id, value):
        if not isinstance(type_id, ObjectIdentifier):
            raise TypeError("type_id must be an ObjectIdentifier")
        if not isinstance(value, bytes):
            raise TypeError("value must be a binary string")

        self._type_id = type_id
        self._value = value

    type_id = utils.read_only_property("_type_id")
    value = utils.read_only_property("_value")

    def __repr__(self):
        return "<OtherName(type_id={0}, value={1!r})>".format(
            self.type_id, self.value)

    def __eq__(self, other):
        if not isinstance(other, OtherName):
            return NotImplemented

        return self.type_id == other.type_id and self.value == other.value

    def __ne__(self, other):
        return not self == other


class GeneralNames(object):
    def __init__(self, general_names):
        if not all(isinstance(x, GeneralName) for x in general_names):
            raise TypeError(
                "Every item in the general_names list must be an "
                "object conforming to the GeneralName interface"
            )

        self._general_names = general_names

    def __iter__(self):
        return iter(self._general_names)

    def __len__(self):
        return len(self._general_names)

    def get_values_for_type(self, type):
        # Return the value of each GeneralName, except for OtherName instances
        # which we return directly because it has two important properties not
        # just one value.
        objs = (i for i in self if isinstance(i, type))
        if type != OtherName:
            objs = (i.value for i in objs)
        return list(objs)

    def __repr__(self):
        return "<GeneralNames({0})>".format(self._general_names)

    def __eq__(self, other):
        if not isinstance(other, GeneralNames):
            return NotImplemented

        return self._general_names == other._general_names

    def __ne__(self, other):
        return not self == other


@utils.register_interface(ExtensionType)
class SubjectAlternativeName(object):
    oid = ExtensionOID.SUBJECT_ALTERNATIVE_NAME

    def __init__(self, general_names):
        self._general_names = GeneralNames(general_names)

    def __iter__(self):
        return iter(self._general_names)

    def __len__(self):
        return len(self._general_names)

    def get_values_for_type(self, type):
        return self._general_names.get_values_for_type(type)

    def __repr__(self):
        return "<SubjectAlternativeName({0})>".format(self._general_names)

    def __eq__(self, other):
        if not isinstance(other, SubjectAlternativeName):
            return NotImplemented

        return self._general_names == other._general_names

    def __ne__(self, other):
        return not self == other


@utils.register_interface(ExtensionType)
class IssuerAlternativeName(object):
    oid = ExtensionOID.ISSUER_ALTERNATIVE_NAME

    def __init__(self, general_names):
        self._general_names = GeneralNames(general_names)

    def __iter__(self):
        return iter(self._general_names)

    def __len__(self):
        return len(self._general_names)

    def get_values_for_type(self, type):
        return self._general_names.get_values_for_type(type)

    def __repr__(self):
        return "<IssuerAlternativeName({0})>".format(self._general_names)

    def __eq__(self, other):
        if not isinstance(other, IssuerAlternativeName):
            return NotImplemented

        return self._general_names == other._general_names

    def __ne__(self, other):
        return not self == other


@utils.register_interface(ExtensionType)
class AuthorityKeyIdentifier(object):
    oid = ExtensionOID.AUTHORITY_KEY_IDENTIFIER

    def __init__(self, key_identifier, authority_cert_issuer,
                 authority_cert_serial_number):
        if authority_cert_issuer or authority_cert_serial_number:
            if not authority_cert_issuer or not authority_cert_serial_number:
                raise ValueError(
                    "authority_cert_issuer and authority_cert_serial_number "
                    "must both be present or both None"
                )

            if not all(
                isinstance(x, GeneralName) for x in authority_cert_issuer
            ):
                raise TypeError(
                    "authority_cert_issuer must be a list of GeneralName "
                    "objects"
                )

            if not isinstance(authority_cert_serial_number, six.integer_types):
                raise TypeError(
                    "authority_cert_serial_number must be an integer"
                )

        self._key_identifier = key_identifier
        self._authority_cert_issuer = authority_cert_issuer
        self._authority_cert_serial_number = authority_cert_serial_number

    @classmethod
    def from_issuer_public_key(cls, public_key):
        digest = _key_identifier_from_public_key(public_key)
        return cls(
            key_identifier=digest,
            authority_cert_issuer=None,
            authority_cert_serial_number=None
        )

    def __repr__(self):
        return (
            "<AuthorityKeyIdentifier(key_identifier={0.key_identifier!r}, "
            "authority_cert_issuer={0.authority_cert_issuer}, "
            "authority_cert_serial_number={0.authority_cert_serial_number}"
            ")>".format(self)
        )

    def __eq__(self, other):
        if not isinstance(other, AuthorityKeyIdentifier):
            return NotImplemented

        return (
            self.key_identifier == other.key_identifier and
            self.authority_cert_issuer == other.authority_cert_issuer and
            self.authority_cert_serial_number ==
            other.authority_cert_serial_number
        )

    def __ne__(self, other):
        return not self == other

    key_identifier = utils.read_only_property("_key_identifier")
    authority_cert_issuer = utils.read_only_property("_authority_cert_issuer")
    authority_cert_serial_number = utils.read_only_property(
        "_authority_cert_serial_number"
    )


@six.add_metaclass(abc.ABCMeta)
class Certificate(object):
    @abc.abstractmethod
    def fingerprint(self, algorithm):
        """
        Returns bytes using digest passed.
        """

    @abc.abstractproperty
    def serial(self):
        """
        Returns certificate serial number
        """

    @abc.abstractproperty
    def version(self):
        """
        Returns the certificate version
        """

    @abc.abstractmethod
    def public_key(self):
        """
        Returns the public key
        """

    @abc.abstractproperty
    def not_valid_before(self):
        """
        Not before time (represented as UTC datetime)
        """

    @abc.abstractproperty
    def not_valid_after(self):
        """
        Not after time (represented as UTC datetime)
        """

    @abc.abstractproperty
    def issuer(self):
        """
        Returns the issuer name object.
        """

    @abc.abstractproperty
    def subject(self):
        """
        Returns the subject name object.
        """

    @abc.abstractproperty
    def signature_hash_algorithm(self):
        """
        Returns a HashAlgorithm corresponding to the type of the digest signed
        in the certificate.
        """

    @abc.abstractproperty
    def extensions(self):
        """
        Returns an Extensions object.
        """

    @abc.abstractmethod
    def __eq__(self, other):
        """
        Checks equality.
        """

    @abc.abstractmethod
    def __ne__(self, other):
        """
        Checks not equal.
        """

    @abc.abstractmethod
    def __hash__(self):
        """
        Computes a hash.
        """

    @abc.abstractmethod
    def public_bytes(self, encoding):
        """
        Serializes the certificate to PEM or DER format.
        """


@six.add_metaclass(abc.ABCMeta)
class CertificateRevocationList(object):

    @abc.abstractmethod
    def fingerprint(self, algorithm):
        """
        Returns bytes using digest passed.
        """

    @abc.abstractproperty
    def signature_hash_algorithm(self):
        """
        Returns a HashAlgorithm corresponding to the type of the digest signed
        in the certificate.
        """

    @abc.abstractproperty
    def issuer(self):
        """
        Returns the X509Name with the issuer of this CRL.
        """

    @abc.abstractproperty
    def next_update(self):
        """
        Returns the date of next update for this CRL.
        """

    @abc.abstractproperty
    def last_update(self):
        """
        Returns the date of last update for this CRL.
        """

    @abc.abstractproperty
    def revoked_certificates(self):
        """
        Returns a list of RevokedCertificate objects for this CRL.
        """

    @abc.abstractproperty
    def extensions(self):
        """
        Returns an Extensions object containing a list of CRL extensions.
        """

    @abc.abstractmethod
    def __eq__(self, other):
        """
        Checks equality.
        """

    @abc.abstractmethod
    def __ne__(self, other):
        """
        Checks not equal.
        """


@six.add_metaclass(abc.ABCMeta)
class CertificateSigningRequest(object):
    @abc.abstractmethod
    def __eq__(self, other):
        """
        Checks equality.
        """

    @abc.abstractmethod
    def __ne__(self, other):
        """
        Checks not equal.
        """

    @abc.abstractmethod
    def __hash__(self):
        """
        Computes a hash.
        """

    @abc.abstractmethod
    def public_key(self):
        """
        Returns the public key
        """

    @abc.abstractproperty
    def subject(self):
        """
        Returns the subject name object.
        """

    @abc.abstractproperty
    def signature_hash_algorithm(self):
        """
        Returns a HashAlgorithm corresponding to the type of the digest signed
        in the certificate.
        """

    @abc.abstractproperty
    def extensions(self):
        """
        Returns the extensions in the signing request.
        """

    @abc.abstractmethod
    def public_bytes(self, encoding):
        """
        Encodes the request to PEM or DER format.
        """


@six.add_metaclass(abc.ABCMeta)
class RevokedCertificate(object):
    @abc.abstractproperty
    def serial_number(self):
        """
        Returns the serial number of the revoked certificate.
        """

    @abc.abstractproperty
    def revocation_date(self):
        """
        Returns the date of when this certificate was revoked.
        """

    @abc.abstractproperty
    def extensions(self):
        """
        Returns an Extensions object containing a list of Revoked extensions.
        """


class CertificateSigningRequestBuilder(object):
    def __init__(self, subject_name=None, extensions=[]):
        """
        Creates an empty X.509 certificate request (v1).
        """
        self._subject_name = subject_name
        self._extensions = extensions

    def subject_name(self, name):
        """
        Sets the certificate requestor's distinguished name.
        """
        if not isinstance(name, Name):
            raise TypeError('Expecting x509.Name object.')
        if self._subject_name is not None:
            raise ValueError('The subject name may only be set once.')
        return CertificateSigningRequestBuilder(name, self._extensions)

    def add_extension(self, extension, critical):
        """
        Adds an X.509 extension to the certificate request.
        """
        if not isinstance(extension, ExtensionType):
            raise TypeError("extension must be an ExtensionType")

        extension = Extension(extension.oid, critical, extension)

        # TODO: This is quadratic in the number of extensions
        for e in self._extensions:
            if e.oid == extension.oid:
                raise ValueError('This extension has already been set.')
        return CertificateSigningRequestBuilder(
            self._subject_name, self._extensions + [extension]
        )

    def sign(self, private_key, algorithm, backend):
        """
        Signs the request using the requestor's private key.
        """
        if self._subject_name is None:
            raise ValueError("A CertificateSigningRequest must have a subject")
        return backend.create_x509_csr(self, private_key, algorithm)


class CertificateBuilder(object):
    def __init__(self, issuer_name=None, subject_name=None,
                 public_key=None, serial_number=None, not_valid_before=None,
                 not_valid_after=None, extensions=[]):
        self._version = Version.v3
        self._issuer_name = issuer_name
        self._subject_name = subject_name
        self._public_key = public_key
        self._serial_number = serial_number
        self._not_valid_before = not_valid_before
        self._not_valid_after = not_valid_after
        self._extensions = extensions

    def issuer_name(self, name):
        """
        Sets the CA's distinguished name.
        """
        if not isinstance(name, Name):
            raise TypeError('Expecting x509.Name object.')
        if self._issuer_name is not None:
            raise ValueError('The issuer name may only be set once.')
        return CertificateBuilder(
            name, self._subject_name, self._public_key,
            self._serial_number, self._not_valid_before,
            self._not_valid_after, self._extensions
        )

    def subject_name(self, name):
        """
        Sets the requestor's distinguished name.
        """
        if not isinstance(name, Name):
            raise TypeError('Expecting x509.Name object.')
        if self._subject_name is not None:
            raise ValueError('The subject name may only be set once.')
        return CertificateBuilder(
            self._issuer_name, name, self._public_key,
            self._serial_number, self._not_valid_before,
            self._not_valid_after, self._extensions
        )

    def public_key(self, key):
        """
        Sets the requestor's public key (as found in the signing request).
        """
        if not isinstance(key, (dsa.DSAPublicKey, rsa.RSAPublicKey,
                                ec.EllipticCurvePublicKey)):
            raise TypeError('Expecting one of DSAPublicKey, RSAPublicKey,'
                            ' or EllipticCurvePublicKey.')
        if self._public_key is not None:
            raise ValueError('The public key may only be set once.')
        return CertificateBuilder(
            self._issuer_name, self._subject_name, key,
            self._serial_number, self._not_valid_before,
            self._not_valid_after, self._extensions
        )

    def serial_number(self, number):
        """
        Sets the certificate serial number.
        """
        if not isinstance(number, six.integer_types):
            raise TypeError('Serial number must be of integral type.')
        if self._serial_number is not None:
            raise ValueError('The serial number may only be set once.')
        if number < 0:
            raise ValueError('The serial number should be non-negative.')
        if utils.bit_length(number) > 160:  # As defined in RFC 5280
            raise ValueError('The serial number should not be more than 160 '
                             'bits.')
        return CertificateBuilder(
            self._issuer_name, self._subject_name,
            self._public_key, number, self._not_valid_before,
            self._not_valid_after, self._extensions
        )

    def not_valid_before(self, time):
        """
        Sets the certificate activation time.
        """
        if not isinstance(time, datetime.datetime):
            raise TypeError('Expecting datetime object.')
        if self._not_valid_before is not None:
            raise ValueError('The not valid before may only be set once.')
        if time <= _UNIX_EPOCH:
            raise ValueError('The not valid before date must be after the unix'
                             ' epoch (1970 January 1).')
        return CertificateBuilder(
            self._issuer_name, self._subject_name,
            self._public_key, self._serial_number, time,
            self._not_valid_after, self._extensions
        )

    def not_valid_after(self, time):
        """
        Sets the certificate expiration time.
        """
        if not isinstance(time, datetime.datetime):
            raise TypeError('Expecting datetime object.')
        if self._not_valid_after is not None:
            raise ValueError('The not valid after may only be set once.')
        if time <= _UNIX_EPOCH:
            raise ValueError('The not valid after date must be after the unix'
                             ' epoch (1970 January 1).')
        return CertificateBuilder(
            self._issuer_name, self._subject_name,
            self._public_key, self._serial_number, self._not_valid_before,
            time, self._extensions
        )

    def add_extension(self, extension, critical):
        """
        Adds an X.509 extension to the certificate.
        """
        if not isinstance(extension, ExtensionType):
            raise TypeError("extension must be an ExtensionType")

        extension = Extension(extension.oid, critical, extension)

        # TODO: This is quadratic in the number of extensions
        for e in self._extensions:
            if e.oid == extension.oid:
                raise ValueError('This extension has already been set.')

        return CertificateBuilder(
            self._issuer_name, self._subject_name,
            self._public_key, self._serial_number, self._not_valid_before,
            self._not_valid_after, self._extensions + [extension]
        )

    def sign(self, private_key, algorithm, backend):
        """
        Signs the certificate using the CA's private key.
        """
        if self._subject_name is None:
            raise ValueError("A certificate must have a subject name")

        if self._issuer_name is None:
            raise ValueError("A certificate must have an issuer name")

        if self._serial_number is None:
            raise ValueError("A certificate must have a serial number")

        if self._not_valid_before is None:
            raise ValueError("A certificate must have a not valid before time")

        if self._not_valid_after is None:
            raise ValueError("A certificate must have a not valid after time")

        if self._public_key is None:
            raise ValueError("A certificate must have a public key")

        return backend.create_x509_certificate(self, private_key, algorithm)