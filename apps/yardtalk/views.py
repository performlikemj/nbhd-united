import stripe
from django.core.exceptions import ImproperlyConfigured
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenants.authentication import PersonalAccessTokenAuthentication
from apps.tenants.permissions import HasYardTalkReadScope

from .serializers import LicenseClaimSerializer, LicenseValidateSerializer
from .services import (
    CheckoutSessionRejected,
    activate_license,
    fulfill_checkout_session,
    user_has_active_subscription,
)
from .throttling import LicenseValidateThrottle


class LicenseValidateView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LicenseValidateThrottle]
    throttle_scope = "yardtalk_license_validate"

    def post(self, request):
        serializer = LicenseValidateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(activate_license(**serializer.validated_data))


class EntitlementView(APIView):
    authentication_classes = [PersonalAccessTokenAuthentication]
    permission_classes = [HasYardTalkReadScope]

    def get(self, request):
        entitled = user_has_active_subscription(request.user)
        response = {
            "entitled": entitled,
            "source": "subscription" if entitled else "none",
        }
        if entitled:
            response["recheck_after_days"] = 7
        return Response(response)


class LicenseClaimView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        serializer = LicenseClaimSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        try:
            license_obj = fulfill_checkout_session(serializer.validated_data["session_id"])
        except CheckoutSessionRejected:
            return Response(
                {"detail": "Checkout Session is not a paid YardTalk purchase."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except (ImproperlyConfigured, stripe.error.StripeError):
            return Response(
                {"detail": "Unable to verify Checkout Session."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"license_key": license_obj.key})
