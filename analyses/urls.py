from django.urls import path

from analyses.views import AnalysisCreateView, AnalysisDetailView

urlpatterns = [
    path('', AnalysisCreateView.as_view(), name='analysis-create'),
    path('<uuid:pk>/', AnalysisDetailView.as_view(), name='analysis-detail'),
]
