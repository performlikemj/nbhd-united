from rest_framework.throttling import ScopedRateThrottle


class LicenseValidateThrottle(ScopedRateThrottle):
    scope = "yardtalk_license_validate"
