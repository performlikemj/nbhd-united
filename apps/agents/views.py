from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import AgentSession, MemoryItem, Message
from .serializers import AgentSessionSerializer, MemoryItemSerializer, MessageSerializer
from .services import AgentRunner


class AgentSessionViewSet(viewsets.ModelViewSet):
    serializer_class = AgentSessionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return AgentSession.objects.filter(tenant_id=self.request.user.tenant_id)

    def perform_create(self, serializer):
        serializer.save(tenant_id=self.request.user.tenant_id)

    @action(detail=True, methods=["post"])
    def send_message(self, request, pk=None):
        """Send a message to the agent and get a response."""
        session = self.get_object()
        content = request.data.get("content", "")
        if not content:
            return Response({"error": "content is required"}, status=status.HTTP_400_BAD_REQUEST)

        runner = AgentRunner()
        response_msg = runner.run(session=session, user_message=content)
        return Response(MessageSerializer(response_msg).data, status=status.HTTP_201_CREATED)


class MessageViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MessageSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Message.objects.filter(
            session__tenant_id=self.request.user.tenant_id
        ).select_related("session")


class MemoryItemViewSet(viewsets.ModelViewSet):
    serializer_class = MemoryItemSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return MemoryItem.objects.filter(tenant_id=self.request.user.tenant_id)

    def perform_create(self, serializer):
        serializer.save(tenant_id=self.request.user.tenant_id)
