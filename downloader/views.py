{% if data %}
<div class="container">
  <div class="media-cards">
    {% for item in images %}
      <div class="card">
        <img src="{{ item.preview }}" alt="image" loading="lazy" />
        <div class="card-actions">
          <a href="{{ item.download }}" class="btn" download>Download</a>
        </div>
      </div>
    {% endfor %}
    {% for item in videos %}
      <div class="card">
        <video controls playsinline>
          <source src="{{ item.preview }}" type="video/mp4" />
        </video>
        <div class="card-actions">
          <a href="{{ item.download }}" class="btn" download>Download</a>
        </div>
      </div>
    {% endfor %}
  </div>
</div>
{% endif %}
