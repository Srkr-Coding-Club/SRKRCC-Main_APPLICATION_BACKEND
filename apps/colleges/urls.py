from django.urls import path

from .views import CollegeDetailView, CollegeListView

urlpatterns = [
    path('', CollegeListView.as_view(), name='college_list'),
    path('<int:pk>/', CollegeDetailView.as_view(), name='college_detail'),
]
