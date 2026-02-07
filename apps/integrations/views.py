from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Integration
from .serializers import IntegrationSerializer


class IntegrationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = IntegrationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Integration.objects.filter(tenant_id=self.request.user.tenant_id)
