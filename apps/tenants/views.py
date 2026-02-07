from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import AgentConfig, Tenant
from .serializers import AgentConfigSerializer, TenantSerializer


class TenantViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TenantSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Tenant.objects.filter(id=self.request.user.tenant_id)


class AgentConfigViewSet(viewsets.ModelViewSet):
    serializer_class = AgentConfigSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return AgentConfig.objects.filter(tenant_id=self.request.user.tenant_id)
