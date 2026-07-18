from rest_framework import serializers

from analyses.models import AnalysisJob
from analyses.validators import TokopediaURLValidationError, normalize_tokopedia_product_url


class AnalysisCreateSerializer(serializers.Serializer):
    url = serializers.CharField(max_length=2_048, trim_whitespace=True)

    def validate_url(self, value: str) -> str:
        try:
            return normalize_tokopedia_product_url(value)
        except TokopediaURLValidationError as exc:
            raise serializers.ValidationError(exc.message, code=exc.code) from exc

    def create(self, validated_data: dict) -> AnalysisJob:
        return AnalysisJob.objects.create(source_url=validated_data['url'])


class AnalysisDetailSerializer(serializers.ModelSerializer):
    sourceUrl = serializers.URLField(source='source_url', read_only=True)
    canonicalUrl = serializers.SerializerMethodField()
    productImageUrl = serializers.SerializerMethodField()
    riskScore = serializers.IntegerField(source='risk_score', read_only=True, allow_null=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)
    startedAt = serializers.DateTimeField(source='started_at', read_only=True, allow_null=True)
    completedAt = serializers.DateTimeField(source='completed_at', read_only=True, allow_null=True)
    error = serializers.SerializerMethodField()

    class Meta:
        model = AnalysisJob
        fields = (
            'id',
            'status',
            'sourceUrl',
            'canonicalUrl',
            'productImageUrl',
            'riskScore',
            'verdict',
            'summary',
            'result',
            'error',
            'createdAt',
            'updatedAt',
            'startedAt',
            'completedAt',
        )
        read_only_fields = fields

    def get_canonicalUrl(self, instance: AnalysisJob) -> str | None:
        return instance.canonical_url or None

    def get_productImageUrl(self, instance: AnalysisJob) -> str | None:
        evidence = instance.evidence if isinstance(instance.evidence, dict) else {}
        product = evidence.get('product', {})
        image_url = product.get('imageUrl') if isinstance(product, dict) else None
        return image_url if isinstance(image_url, str) and image_url else None

    def get_error(self, instance: AnalysisJob) -> dict[str, str] | None:
        if instance.status != AnalysisJob.Status.FAILED:
            return None
        return {
            'code': instance.error_code or 'ANALYSIS_FAILED',
            'message': instance.error_message or 'Analisis tidak dapat diselesaikan.',
        }

    def to_representation(self, instance: AnalysisJob) -> dict:
        data = super().to_representation(instance)
        data['riskScore'] = instance.risk_score
        data['verdict'] = instance.verdict or None
        data['summary'] = instance.summary or None
        data['result'] = instance.result or None
        return data
