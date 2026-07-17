from django.urls import reverse
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from analyses.models import AnalysisJob
from analyses.serializers import AnalysisCreateSerializer, AnalysisDetailSerializer


class AnalysisCreateView(generics.CreateAPIView):
    serializer_class = AnalysisCreateSerializer
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'analysis_create'

    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        analysis = serializer.save()

        detail_path = reverse('analysis-detail', kwargs={'pk': analysis.pk})
        response_data = AnalysisDetailSerializer(analysis).data
        response_data['statusUrl'] = detail_path
        return Response(
            response_data,
            status=status.HTTP_202_ACCEPTED,
            headers={'Location': detail_path},
        )


class AnalysisDetailView(generics.RetrieveAPIView):
    queryset = AnalysisJob.objects.all()
    serializer_class = AnalysisDetailSerializer
