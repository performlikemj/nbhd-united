from rest_framework import serializers


class LicenseValidateSerializer(serializers.Serializer):
    license_key = serializers.RegexField(
        r"^YT-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$",
        max_length=17,
    )
    device_id = serializers.CharField(min_length=1, max_length=64, trim_whitespace=False)


class LicenseClaimSerializer(serializers.Serializer):
    session_id = serializers.CharField(min_length=1, max_length=255, trim_whitespace=False)
