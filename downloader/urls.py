from django.urls import path
from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("posts", views.posts, name="posts"),
    path("reels", views.reels, name="reels"),
    path("allposts", views.allposts, name="allposts"),
]
