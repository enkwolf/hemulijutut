function isElementInViewport (el) {
        //special bonus for those using jQuery
        if (el instanceof jQuery) {
          el = el[0];
        }

        var rect = el.getBoundingClientRect();

        var result = (rect.top >= 0 &&
          rect.bottom <= $(window).height());
        return result;
      }

      var handler = function() {
        var headings = $(".content-heading");
        var topmost_found = false;
        headings.each(function(index) {
          var id = $(this).find("span.anchor-offset").first().attr("id");
          var link_in_toc = $("#toc li a[href=#" + id + "]").parent();
          if (isElementInViewport(this) && topmost_found == false) {
            $(link_in_toc).attr("class", "toc-visible");
            topmost_found = true;
          } else {
            $(link_in_toc).attr("class", "");
          }
        });
      };


// Build the table of contents
function build_toc(static_root_url) {
        var toc = $("nav.toc > div.list-div > ol");
        var current_toc_level = 1;
        //var topmost_ol = null;
        var headings = $.map([1, 2, 3, 4, 5, 6], function(i) {
          return "section.content h" + i + ".content-heading";
        }).join(", ");
        $(headings).each(function(index) {
          // TODO: http://stackoverflow.com/questions/123999/how-to-tell-if-a-dom-element-is-visible-in-the-current-viewport
          var new_toc_level = parseInt(this.tagName[1]);
          
          // TODO: Fix, ol can only contain li
          if (new_toc_level > current_toc_level) {
            for (var i = current_toc_level; i < new_toc_level; i += 1) { // >
              //var new_li = document.createElement("li");
              var new_ol = document.createElement("ol");
              toc.append(new_ol);
              //$(new_li).append(new_ol);
              //$(new_li).attr("class", "ol-container");

              toc = $(new_ol);
            }
          } else if (new_toc_level < current_toc_level) { // >
            for (var i = current_toc_level; i > new_toc_level; i -= 1) {
              toc = toc.parent();
            }
          }
          current_toc_level = new_toc_level;

          var li = document.createElement("li");
          var text = this.firstChild.nodeValue;
          var id = $(this).find("span.anchor-offset").first().attr("id");
          var anchor = document.createElement("a");
          $(anchor).attr("href", "#" + id)
          $(anchor).text(text);
          
          var h_parent = this.parentNode.parentNode;
          var icon, icon_img;
          if (h_parent.className == "embedded-page") {
            var status = $(h_parent).find("img").first().attr("class");
            if (status == "correct")
              icon = static_root_url + "correct-16.png";
            else if (status == "incorrect")
              icon = static_root_url + "incorrect-16.png";
            else if (status == "unanswered")
              icon = static_root_url + "unanswered-16.png";
            icon_img = $(document.createElement("img"));
            icon_img.attr("src", icon);
          }
          $(li).append(anchor);
          if (icon)
            $(li).append(icon_img);

          toc.append(li);
        });
}