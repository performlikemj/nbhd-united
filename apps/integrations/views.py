"""Integration views — list, connect (OAuth callback), disconnect."""
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Integration
from .serializers import IntegrationSerializer
from .services import connect_integration, disconnect_integration


class IntegrationViewSet(viewsets.ReadOnlyModelViewSet):
    """List and manage integrations for the current tenant."""
    serializer_class = IntegrationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if hasattr(self.request.user, "tenant"):
            return Integration.objects.filter(tenant=self.request.user.tenant)
        return Integration.objects.none()

    @action(detail=True, methods=["post"], url_path="disconnect")
    def disconnect(self, request, pk=None):
        """Disconnect an integration — revokes tokens."""
        integration = self.get_object()
        disconnect_integration(integration.tenant, integration.provider)
        return Response({"status": "disconnected"}, status=status.HTTP_200_OK)
