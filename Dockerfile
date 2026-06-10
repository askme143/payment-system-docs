FROM nginx:stable-alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY *.html /usr/share/nginx/html/
COPY diagrams/ /usr/share/nginx/html/diagrams/
COPY docs-data/ /usr/share/nginx/html/docs-data/

