from django.urls import path

from . import views

app_name = "recipes"

urlpatterns = [
    path("", views.RecipeListView.as_view(), name="recipe_list"),
    path("new/", views.recipe_create, name="recipe_create"),
    path("<int:pk>/", views.recipe_detail, name="recipe_detail"),
    path("<int:pk>/edit/", views.recipe_update, name="recipe_update"),
    path("<int:pk>/delete/", views.recipe_delete, name="recipe_delete"),
]
