from django.contrib import admin

from analyses.models import AnalysisJob


@admin.register(AnalysisJob)
class AnalysisJobAdmin(admin.ModelAdmin):
    list_display = ('id', 'status', 'risk_score', 'created_at', 'updated_at')
    list_filter = ('status', 'created_at')
    search_fields = ('id', 'source_url', 'canonical_url')
    readonly_fields = ('id', 'created_at', 'updated_at')
