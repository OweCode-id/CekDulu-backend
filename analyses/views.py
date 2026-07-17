import logging

from django.urls import reverse
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from analyses.models import AnalysisJob
from analyses.serializers import AnalysisCreateSerializer, AnalysisDetailSerializer
from analyses.tasks import collect_analysis_evidence

logger = logging.getLogger(__name__)


class AnalysisCreateView(generics.CreateAPIView):
    serializer_class = AnalysisCreateSerializer
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'analysis_create'

    def create(self, request: Request, *args, **kwargs) -> Response:
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        analysis = serializer.save()

        detail_path = reverse('analysis-detail', kwargs={'pk': analysis.pk})
        try:
            collect_analysis_evidence.delay(str(analysis.pk))
        except Exception:
            logger.exception('Failed to enqueue analysis %s.', analysis.pk)
            now = timezone.now()
            AnalysisJob.objects.filter(
                pk=analysis.pk,
                status=AnalysisJob.Status.QUEUED,
            ).update(
                status=AnalysisJob.Status.FAILED,
                error_code='QUEUE_UNAVAILABLE',
                error_message='Layanan analisis sementara tidak tersedia.',
                completed_at=now,
                updated_at=now,
            )
            analysis.refresh_from_db()
            response_data = AnalysisDetailSerializer(analysis).data
            response_data['statusUrl'] = detail_path
            return Response(
                response_data,
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
                headers={'Location': detail_path},
            )

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
