from django.urls import path

from . import views

app_name = "margins"

urlpatterns = [
    path("", views.margins_home, name="margins_home"),
    # « Compter dans la marge produits », a category at a time. POST only; a
    # GET goes back to the page.
    path("articles/", views.count_articles, name="count_articles"),
]
